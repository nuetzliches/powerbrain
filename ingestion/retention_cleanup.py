"""
Retention Service: Automatic Data Deletion
==============================================
Runs as a cron job (e.g. daily) and deletes data
whose retention period has expired.

Deletes in a coordinated manner from: PostgreSQL, Qdrant, and anonymizes audit logs.

Usage:
    python retention_cleanup.py              # Dry run (report only)
    python retention_cleanup.py --execute    # Actually delete
"""

import asyncio
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointIdsList

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.config import build_postgres_url, PG_POOL_MIN, PG_POOL_MAX

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("retention")

POSTGRES_URL = build_postgres_url()
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))


async def get_expiring_data(pool: asyncpg.Pool) -> list[dict]:
    """Finds all records with expired retention periods."""
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
    """Deletes a dataset from PostgreSQL and associated vectors from Qdrant."""
    report = {"dataset_id": dataset_id, "actions": []}

    # 1. Find and delete Qdrant vectors
    doc = await pool.fetchrow(
        "SELECT qdrant_collection FROM documents_meta WHERE id = $1", dataset_id
    )
    if doc and doc["qdrant_collection"]:
        collection = doc["qdrant_collection"]
        # Find points with this dataset reference
        # (In practice: payload filter on dataset_id)
        report["actions"].append(f"Qdrant: vectors in collection '{collection}' marked")

        if execute:
            try:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                # Scroll through all points with this dataset
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
                    report["actions"].append(f"Qdrant: {len(point_ids)} vectors deleted")
            except Exception as e:
                report["actions"].append(f"Qdrant error: {e}")

    # 2. PostgreSQL: Delete dataset rows
    if execute:
        deleted = await pool.execute(
            "DELETE FROM dataset_rows WHERE dataset_id = $1", dataset_id
        )
        report["actions"].append(f"PostgreSQL: dataset_rows deleted ({deleted})")

        await pool.execute("DELETE FROM documents_meta WHERE id = $1", dataset_id)
        report["actions"].append("PostgreSQL: documents_meta deleted")

        await pool.execute("DELETE FROM datasets WHERE id = $1", dataset_id)
        report["actions"].append("PostgreSQL: datasets deleted")
    else:
        count = await pool.fetchval(
            "SELECT count(*) FROM dataset_rows WHERE dataset_id = $1", dataset_id
        )
        report["actions"].append(f"[DRY-RUN] Would delete {count} rows")

    # 3. Audit log: Anonymize PII (do not delete — accountability requirement)
    if execute:
        await pool.execute("""
            UPDATE agent_access_log
            SET request_context = '{"anonymized": true}'::jsonb
            WHERE resource_id = $1 AND contains_pii = true
        """, dataset_id)
        report["actions"].append("Audit log: PII entries anonymized")

    return report


async def process_deletion_requests(pool: asyncpg.Pool, qdrant: AsyncQdrantClient,
                                     execute: bool) -> list[dict]:
    """Processes pending deletion requests (Art. 17 GDPR)."""
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

        # Process all linked datasets
        if req["datasets"]:
            for ds_id in req["datasets"]:
                ds_report = await delete_dataset(pool, qdrant, str(ds_id), execute)
                report["actions"].extend(ds_report["actions"])

        # Delete Qdrant points directly
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
                        report["actions"].append(f"Qdrant: point {parts[1]} from {parts[0]} deleted")
                    except Exception as e:
                        report["actions"].append(f"Qdrant error: {e}")

        # Vault: Delete original + mapping (tier 1: restrict)
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

            # restrict: Keep Qdrant points, but contains_pii → false
            # (Pseudonyms are now irreversible = de facto anonymous)
            if vault_deleted > 0:
                for ds_id in dataset_ids:
                    await pool.execute("""
                        UPDATE documents_meta SET contains_pii = false
                        WHERE id = $1
                    """, ds_id)

            report["actions"].append(
                f"Vault: {vault_deleted} original entries, "
                f"{mapping_deleted} mappings deleted (restrict)"
            )
        elif dataset_ids:
            report["actions"].append(
                f"[DRY-RUN] Would delete vault entries for {len(dataset_ids)} documents (restrict)"
            )

        # Update status
        if execute:
            await pool.execute("""
                UPDATE deletion_requests
                SET status = 'completed',
                    completed_at = now(),
                    deleted_records = $2
                WHERE id = $1
            """, req["id"], json.dumps(report["actions"]))
            report["actions"].append("Deletion request marked as completed")

        reports.append(report)

    return reports


async def clean_expired_vault(conn, dry_run: bool = True) -> dict:
    """
    Deletes expired vault entries and associated mappings.
    Returns statistics.
    """
    stats = {"expired_content": 0, "expired_mappings": 0, "orphaned": 0}

    # 1. Find expired vault entries
    expired = await conn.fetch("""
        SELECT id, document_id, chunk_index
        FROM pii_vault.original_content
        WHERE retention_expires_at <= now()
    """)
    stats["expired_content"] = len(expired)

    if expired and not dry_run:
        expired_ids = [r["id"] for r in expired]
        expired_doc_chunks = [(r["document_id"], r["chunk_index"]) for r in expired]

        # Delete mappings
        for doc_id, chunk_idx in expired_doc_chunks:
            deleted = await conn.execute("""
                DELETE FROM pii_vault.pseudonym_mapping
                WHERE document_id = $1 AND chunk_index = $2
            """, doc_id, chunk_idx)
            stats["expired_mappings"] += int(deleted.split()[-1]) if deleted else 0

        # Delete original content
        await conn.execute("""
            DELETE FROM pii_vault.original_content
            WHERE id = ANY($1::uuid[])
        """, expired_ids)

        log.info(
            f"Vault Cleanup: {stats['expired_content']} expired entries deleted, "
            f"{stats['expired_mappings']} mappings removed"
        )

    # 2. Orphaned vault entries (document_meta deleted, vault still present)
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
    Anonymizes audit log entries whose retention period has expired.
    Sets request_context to '{"anonymized": true}' for entries older than
    AUDIT_RETENTION_DAYS days.
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
        report["actions"].append(f"Audit log: {count} entries anonymized (>{AUDIT_RETENTION_DAYS} days)")
    else:
        report["actions"].append(f"[DRY-RUN] Would anonymize {count} audit entries (>{AUDIT_RETENTION_DAYS} days)")

    return report


async def main():
    parser = argparse.ArgumentParser(description="Retention Cleanup Service")
    parser.add_argument("--execute", action="store_true", help="Actually delete (without: dry run)")
    args = parser.parse_args()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    log.info(f"Retention cleanup started [{mode}]")

    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX)
    qdrant = AsyncQdrantClient(url=QDRANT_URL)

    # 1. Expired retention periods
    log.info("Checking expired retention periods...")
    expiring = await get_expiring_data(pool)
    log.info(f"  {len(expiring)} expired records found")

    for item in expiring:
        log.info(f"  → {item['source_type']} '{item['title']}' "
                 f"(category: {item['data_category']}, PII: {item['contains_pii']})")
        if item["source_type"] == "dataset":
            report = await delete_dataset(pool, qdrant, str(item["id"]), args.execute)
            for action in report["actions"]:
                log.info(f"    {action}")

    # 2. Deletion requests (Art. 17)
    log.info("Checking pending deletion requests...")
    deletion_reports = await process_deletion_requests(pool, qdrant, args.execute)
    log.info(f"  {len(deletion_reports)} deletion requests processed")

    for report in deletion_reports:
        log.info(f"  → Data subject: {report['subject']}")
        for action in report["actions"]:
            log.info(f"    {action}")

    # 3. Vault cleanup: Expired + orphaned entries
    log.info("=== Phase 3: Vault Retention + Orphan Cleanup ===")
    async with pool.acquire() as conn:
        vault_stats = await clean_expired_vault(conn, dry_run=not args.execute)
    log.info(
        f"  Vault: {vault_stats['expired_content']} expired, "
        f"{vault_stats['orphaned']} orphaned"
        f"{' (dry-run)' if not args.execute else ' → deleted'}"
    )

    # 4. Audit log: Time-based anonymization
    log.info("=== Phase 4: Audit-Log Retention ===")
    audit_report = await anonymize_old_audit_logs(pool, args.execute)
    for action in audit_report["actions"]:
        log.info(f"  {action}")

    await pool.close()
    log.info(f"Retention cleanup completed [{mode}]")


if __name__ == "__main__":
    asyncio.run(main())
