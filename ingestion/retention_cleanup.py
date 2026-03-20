"""
Retention-Service: Automatische Datenlöschung
==============================================
Wird als Cronjob ausgeführt (z.B. täglich) und löscht Daten,
deren Aufbewahrungsfrist abgelaufen ist.

Löscht koordiniert aus: PostgreSQL, Qdrant und anonymisiert Audit-Logs.

Aufruf:
    python retention_cleanup.py              # Dry-Run (nur Report)
    python retention_cleanup.py --execute    # Tatsächlich löschen
"""

import asyncio
import argparse
import json
import logging
import os
from datetime import datetime, timezone

import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointIdsList

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("retention")

POSTGRES_URL = "postgresql://kb_admin:changeme@localhost:5432/knowledgebase"
QDRANT_URL = "http://localhost:6333"
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))


async def get_expiring_data(pool: asyncpg.Pool) -> list[dict]:
    """Findet alle Datensätze mit abgelaufener Aufbewahrungsfrist."""
    rows = await pool.fetch("""
        SELECT source_type, id, title, data_category, contains_pii,
               retention_expires_at, time_remaining
        FROM v_expiring_data
        WHERE time_remaining <= INTERVAL '0 days'
        ORDER BY retention_expires_at ASC
    """)
    return [dict(r) for r in rows]


async def delete_dataset(pool: asyncpg.Pool, qdrant: AsyncQdrantClient,
                         dataset_id: str, execute: bool) -> dict:
    """Löscht einen Datensatz aus PostgreSQL und zugehörige Vektoren aus Qdrant."""
    report = {"dataset_id": dataset_id, "actions": []}

    # 1. Qdrant-Vektoren finden und löschen
    doc = await pool.fetchrow(
        "SELECT qdrant_collection FROM documents_meta WHERE id = $1", dataset_id
    )
    if doc and doc["qdrant_collection"]:
        collection = doc["qdrant_collection"]
        # Finde Punkte mit dieser Dataset-Referenz
        # (In der Praxis: Payload-Filter auf dataset_id)
        report["actions"].append(f"Qdrant: Vektoren in Collection '{collection}' markiert")

        if execute:
            try:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                # Scroll durch alle Punkte mit diesem Dataset
                points, _ = await qdrant.scroll(
                    collection_name=collection,
                    scroll_filter=Filter(must=[
                        FieldCondition(key="dataset_id", match=MatchValue(value=dataset_id))
                    ]),
                    limit=10000,
                )
                if points:
                    point_ids = [p.id for p in points]
                    await qdrant.delete(
                        collection_name=collection,
                        points_selector=PointIdsList(points=point_ids),
                    )
                    report["actions"].append(f"Qdrant: {len(point_ids)} Vektoren gelöscht")
            except Exception as e:
                report["actions"].append(f"Qdrant-Fehler: {e}")

    # 2. PostgreSQL: Datensatz-Zeilen löschen
    if execute:
        deleted = await pool.execute(
            "DELETE FROM dataset_rows WHERE dataset_id = $1", dataset_id
        )
        report["actions"].append(f"PostgreSQL: dataset_rows gelöscht ({deleted})")

        await pool.execute("DELETE FROM documents_meta WHERE id = $1", dataset_id)
        report["actions"].append("PostgreSQL: documents_meta gelöscht")

        await pool.execute("DELETE FROM datasets WHERE id = $1", dataset_id)
        report["actions"].append("PostgreSQL: datasets gelöscht")
    else:
        count = await pool.fetchval(
            "SELECT count(*) FROM dataset_rows WHERE dataset_id = $1", dataset_id
        )
        report["actions"].append(f"[DRY-RUN] Würde {count} Zeilen löschen")

    # 3. Audit-Log: PII anonymisieren (nicht löschen — Nachweispflicht)
    if execute:
        await pool.execute("""
            UPDATE agent_access_log
            SET request_context = '{"anonymized": true}'::jsonb
            WHERE resource_id = $1 AND contains_pii = true
        """, dataset_id)
        report["actions"].append("Audit-Log: PII-Einträge anonymisiert")

    return report


async def process_deletion_requests(pool: asyncpg.Pool, qdrant: AsyncQdrantClient,
                                     execute: bool) -> list[dict]:
    """Verarbeitet offene Löschanfragen (Art. 17 DSGVO)."""
    requests = await pool.fetch("""
        SELECT dr.id, dr.data_subject_id, ds.external_ref, ds.datasets, ds.qdrant_point_ids
        FROM deletion_requests dr
        JOIN data_subjects ds ON dr.data_subject_id = ds.id
        WHERE dr.status = 'pending'
        ORDER BY dr.request_date ASC
    """)

    reports = []
    for req in requests:
        report = {"request_id": str(req["id"]), "subject": req["external_ref"], "actions": []}

        # Alle verknüpften Datensätze durchgehen
        if req["datasets"]:
            for ds_id in req["datasets"]:
                ds_report = await delete_dataset(pool, qdrant, str(ds_id), execute)
                report["actions"].extend(ds_report["actions"])

        # Qdrant-Punkte direkt löschen
        if req["qdrant_point_ids"] and execute:
            for collection_points in req["qdrant_point_ids"]:
                # Format: "collection:point_id"
                parts = collection_points.split(":", 1)
                if len(parts) == 2:
                    try:
                        await qdrant.delete(
                            collection_name=parts[0],
                            points_selector=PointIdsList(points=[parts[1]]),
                        )
                        report["actions"].append(f"Qdrant: Punkt {parts[1]} aus {parts[0]} gelöscht")
                    except Exception as e:
                        report["actions"].append(f"Qdrant-Fehler: {e}")

        # Vault: Original + Mapping löschen (Stufe 1: restrict)
        vault_deleted = 0
        mapping_deleted = 0
        dataset_ids = [str(ds_id) for ds_id in req["datasets"]] if req["datasets"] else []
        if execute and dataset_ids:
            for ds_id in dataset_ids:
                m_result = await pool.execute("""
                    DELETE FROM pii_vault.pseudonym_mapping
                    WHERE document_id IN (
                        SELECT id FROM documents_meta WHERE id = $1
                    )
                """, ds_id)
                mapping_deleted += int(m_result.split()[-1]) if m_result else 0

                v_result = await pool.execute("""
                    DELETE FROM pii_vault.original_content
                    WHERE document_id IN (
                        SELECT id FROM documents_meta WHERE id = $1
                    )
                """, ds_id)
                vault_deleted += int(v_result.split()[-1]) if v_result else 0

            # restrict: Qdrant-Punkte behalten, aber contains_pii → false
            # (Pseudonyme sind jetzt irreversibel = de facto anonym)
            if vault_deleted > 0:
                for ds_id in dataset_ids:
                    await pool.execute("""
                        UPDATE documents_meta SET contains_pii = false
                        WHERE id = $1
                    """, ds_id)

            report["actions"].append(
                f"Vault: {vault_deleted} original entries, "
                f"{mapping_deleted} mappings gelöscht (restrict)"
            )
        elif dataset_ids:
            report["actions"].append(
                f"[DRY-RUN] Würde Vault-Einträge für {len(dataset_ids)} Dokumente löschen (restrict)"
            )

        # Status aktualisieren
        if execute:
            await pool.execute("""
                UPDATE deletion_requests
                SET status = 'completed',
                    completed_at = now(),
                    deleted_records = $2
                WHERE id = $1
            """, req["id"], json.dumps(report["actions"]))
            report["actions"].append("Löschanfrage als abgeschlossen markiert")

        reports.append(report)

    return reports


async def clean_expired_vault(conn, dry_run: bool = True) -> dict:
    """
    Löscht abgelaufene Vault-Einträge und zugehörige Mappings.
    Gibt Statistiken zurück.
    """
    stats = {"expired_content": 0, "expired_mappings": 0, "orphaned": 0}

    # 1. Abgelaufene Vault-Einträge finden
    expired = await conn.fetch("""
        SELECT id, document_id, chunk_index
        FROM pii_vault.original_content
        WHERE retention_expires_at <= now()
    """)
    stats["expired_content"] = len(expired)

    if expired and not dry_run:
        expired_ids = [r["id"] for r in expired]
        expired_doc_chunks = [(r["document_id"], r["chunk_index"]) for r in expired]

        # Mappings löschen
        for doc_id, chunk_idx in expired_doc_chunks:
            deleted = await conn.execute("""
                DELETE FROM pii_vault.pseudonym_mapping
                WHERE document_id = $1 AND chunk_index = $2
            """, doc_id, chunk_idx)
            stats["expired_mappings"] += int(deleted.split()[-1]) if deleted else 0

        # Original-Content löschen
        await conn.execute("""
            DELETE FROM pii_vault.original_content
            WHERE id = ANY($1::uuid[])
        """, expired_ids)

        log.info(
            f"Vault Cleanup: {stats['expired_content']} expired entries deleted, "
            f"{stats['expired_mappings']} mappings removed"
        )

    # 2. Verwaiste Vault-Einträge (document_meta gelöscht, Vault noch da)
    orphaned = await conn.fetch("""
        SELECT oc.id, oc.document_id
        FROM pii_vault.original_content oc
        LEFT JOIN documents_meta dm ON dm.id = oc.document_id
        WHERE dm.id IS NULL
    """)
    stats["orphaned"] = len(orphaned)

    if orphaned and not dry_run:
        orphan_ids = [r["id"] for r in orphaned]
        orphan_doc_ids = list({r["document_id"] for r in orphaned})

        for doc_id in orphan_doc_ids:
            await conn.execute("""
                DELETE FROM pii_vault.pseudonym_mapping
                WHERE document_id = $1
            """, doc_id)

        await conn.execute("""
            DELETE FROM pii_vault.original_content
            WHERE id = ANY($1::uuid[])
        """, orphan_ids)

        log.info(f"Vault Cleanup: {stats['orphaned']} orphaned entries removed")

    return stats


async def anonymize_old_audit_logs(pool: asyncpg.Pool, execute: bool) -> dict:
    """
    Anonymisiert Audit-Log-Einträge, deren Aufbewahrungsfrist abgelaufen ist.
    Setzt request_context auf '{"anonymized": true}' für Einträge älter als
    AUDIT_RETENTION_DAYS Tage.
    """
    count = await pool.fetchval("""
        SELECT count(*) FROM agent_access_log
        WHERE created_at < now() - interval '1 day' * $1
          AND request_context != '{"anonymized": true}'::jsonb
    """, AUDIT_RETENTION_DAYS)

    report = {
        "retention_days": AUDIT_RETENTION_DAYS,
        "entries_to_anonymize": count,
        "actions": [],
    }

    if execute and count > 0:
        await pool.execute("""
            UPDATE agent_access_log
            SET request_context = '{"anonymized": true}'::jsonb
            WHERE created_at < now() - interval '1 day' * $1
              AND request_context != '{"anonymized": true}'::jsonb
        """, AUDIT_RETENTION_DAYS)
        report["actions"].append(f"Audit-Log: {count} Einträge anonymisiert (>{AUDIT_RETENTION_DAYS} Tage)")
    else:
        report["actions"].append(f"[DRY-RUN] Würde {count} Audit-Einträge anonymisieren (>{AUDIT_RETENTION_DAYS} Tage)")

    return report


async def main():
    parser = argparse.ArgumentParser(description="Retention Cleanup Service")
    parser.add_argument("--execute", action="store_true", help="Tatsächlich löschen (ohne: Dry-Run)")
    args = parser.parse_args()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    log.info(f"Retention Cleanup gestartet [{mode}]")

    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=5)
    qdrant = AsyncQdrantClient(url=QDRANT_URL)

    # 1. Abgelaufene Aufbewahrungsfristen
    log.info("Prüfe abgelaufene Aufbewahrungsfristen...")
    expiring = await get_expiring_data(pool)
    log.info(f"  {len(expiring)} abgelaufene Datensätze gefunden")

    for item in expiring:
        log.info(f"  → {item['source_type']} '{item['title']}' "
                 f"(Kategorie: {item['data_category']}, PII: {item['contains_pii']})")
        if item["source_type"] == "dataset":
            report = await delete_dataset(pool, qdrant, str(item["id"]), args.execute)
            for action in report["actions"]:
                log.info(f"    {action}")

    # 2. Löschanfragen (Art. 17)
    log.info("Prüfe offene Löschanfragen...")
    deletion_reports = await process_deletion_requests(pool, qdrant, args.execute)
    log.info(f"  {len(deletion_reports)} Löschanfragen verarbeitet")

    for report in deletion_reports:
        log.info(f"  → Betroffene Person: {report['subject']}")
        for action in report["actions"]:
            log.info(f"    {action}")

    # 3. Vault-Cleanup: Abgelaufene + verwaiste Einträge
    log.info("=== Phase 3: Vault Retention + Orphan Cleanup ===")
    async with pool.acquire() as conn:
        vault_stats = await clean_expired_vault(conn, dry_run=not args.execute)
    log.info(
        f"  Vault: {vault_stats['expired_content']} expired, "
        f"{vault_stats['orphaned']} orphaned"
        f"{' (dry-run)' if not args.execute else ' → gelöscht'}"
    )

    # 4. Audit-Log: Zeitbasierte Anonymisierung
    log.info("=== Phase 4: Audit-Log Retention ===")
    audit_report = await anonymize_old_audit_logs(pool, args.execute)
    for action in audit_report["actions"]:
        log.info(f"  {action}")

    await pool.close()
    log.info(f"Retention Cleanup abgeschlossen [{mode}]")


if __name__ == "__main__":
    asyncio.run(main())
