"""
Snapshot-Service (Baustein 4: Wissens-Versionierung)
======================================================
Erstellt und verwaltet Snapshots der Wissensdatenbank:
  - Qdrant: native Snapshot-API für alle Collections
  - PostgreSQL: Row-Counts + Metadaten
  - OPA: aktueller Policy Commit-Hash aus Forgejo

Kann als Modul (vom Ingestion-API aufgerufen) oder direkt per CLI
verwendet werden:

  python snapshot_service.py --auto          # täglicher Snapshot + Cleanup
  python snapshot_service.py --list          # Snapshots auflisten
  python snapshot_service.py --name my-snap  # benannten Snapshot erstellen
"""

import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timezone

import httpx
import asyncpg

log = logging.getLogger("pb-snapshot")

# ── Konfiguration ────────────────────────────────────────────
QDRANT_URL   = os.getenv("QDRANT_URL",   "http://qdrant:6333")
POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://pb_admin:changeme@postgres:5432/powerbrain")
FORGEJO_URL  = os.getenv("FORGEJO_URL",  "http://forgejo.local:3000")
FORGEJO_TOKEN = os.getenv("FORGEJO_TOKEN", "")

QDRANT_COLLECTIONS = ["pb_general", "pb_code", "pb_rules"]
PG_SNAPSHOT_TABLES = ["datasets", "dataset_rows", "documents_meta"]
KEEP_LAST_N        = int(os.getenv("SNAPSHOT_KEEP_LAST_N", "10"))


# ── Qdrant-Snapshots ─────────────────────────────────────────

async def create_qdrant_snapshots(client: httpx.AsyncClient) -> dict:
    """Erstellt Qdrant-Snapshots für alle Collections. Gibt {collection: snapshot_name} zurück."""
    snapshots = {}
    for collection in QDRANT_COLLECTIONS:
        try:
            resp = await client.post(f"{QDRANT_URL}/collections/{collection}/snapshots")
            if resp.status_code == 404:
                log.warning(f"Collection '{collection}' nicht gefunden, überspringe")
                continue
            resp.raise_for_status()
            snap_name = resp.json()["result"]["name"]
            snapshots[collection] = snap_name
            log.info(f"Qdrant-Snapshot erstellt: {collection} → {snap_name}")
        except Exception as e:
            log.error(f"Qdrant-Snapshot für '{collection}' fehlgeschlagen: {e}")
    return snapshots


async def list_qdrant_snapshots(client: httpx.AsyncClient, collection: str) -> list[dict]:
    """Listet vorhandene Qdrant-Snapshots für eine Collection."""
    try:
        resp = await client.get(f"{QDRANT_URL}/collections/{collection}/snapshots")
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        log.error(f"Qdrant-Snapshots auflisten für '{collection}' fehlgeschlagen: {e}")
        return []


async def delete_qdrant_snapshot(client: httpx.AsyncClient, collection: str, snapshot_name: str):
    """Löscht einen Qdrant-Snapshot."""
    try:
        resp = await client.delete(
            f"{QDRANT_URL}/collections/{collection}/snapshots/{snapshot_name}"
        )
        resp.raise_for_status()
        log.info(f"Qdrant-Snapshot gelöscht: {collection}/{snapshot_name}")
    except Exception as e:
        log.error(f"Qdrant-Snapshot löschen fehlgeschlagen: {e}")


async def restore_qdrant_snapshot(client: httpx.AsyncClient, collection: str, snapshot_name: str):
    """Stellt eine Qdrant-Collection aus einem Snapshot wieder her."""
    resp = await client.put(
        f"{QDRANT_URL}/collections/{collection}/snapshots/recover",
        json={"location": f"file:///qdrant/snapshots/{collection}/{snapshot_name}"}
    )
    resp.raise_for_status()
    log.info(f"Qdrant-Collection '{collection}' aus Snapshot '{snapshot_name}' wiederhergestellt")


# ── PostgreSQL Row-Counts ─────────────────────────────────────

async def get_pg_row_counts(pool: asyncpg.Pool) -> dict:
    """Gibt aktuelle Row-Counts der relevanten Tabellen zurück."""
    counts = {}
    for table in PG_SNAPSHOT_TABLES:
        try:
            row = await pool.fetchrow(f"SELECT COUNT(*) AS cnt FROM {table}")
            counts[table] = row["cnt"] if row else 0
        except Exception:
            counts[table] = -1  # Tabelle existiert (noch) nicht
    return counts


# ── OPA Policy Commit ─────────────────────────────────────────

async def get_policy_commit(client: httpx.AsyncClient) -> str | None:
    """Holt den aktuellen Commit-Hash des Policy-Repos aus Forgejo."""
    if not FORGEJO_TOKEN:
        return None
    try:
        headers = {"Authorization": f"token {FORGEJO_TOKEN}"}
        resp = await client.get(
            f"{FORGEJO_URL}/api/v1/repos/pb-org/pb-policies/branches/main",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()["commit"]["id"]
    except Exception as e:
        log.warning(f"Policy-Commit nicht abrufbar: {e}")
        return None


# ── Snapshot erstellen ────────────────────────────────────────

async def create_snapshot(name: str, description: str = "",
                          created_by: str = "system") -> dict:
    """
    Erstellt einen vollständigen Wissens-Snapshot:
    1. Qdrant-Snapshots für alle Collections
    2. PostgreSQL Row-Counts + Zeitstempel
    3. OPA Policy Commit-Hash

    Speichert Metadaten in `knowledge_snapshots`.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)

        try:
            qdrant_snaps  = await create_qdrant_snapshots(client)
            pg_counts     = await get_pg_row_counts(pool)
            policy_commit = await get_policy_commit(client)

            components = {
                "qdrant": {
                    "collections": qdrant_snaps,
                },
                "postgres": {
                    "tables": PG_SNAPSHOT_TABLES,
                    "row_counts": pg_counts,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                },
                "opa": {
                    "policy_commit": policy_commit,
                    "forgejo_url": FORGEJO_URL,
                },
            }

            row = await pool.fetchrow("""
                INSERT INTO knowledge_snapshots
                    (snapshot_name, created_by, description, components, status)
                VALUES ($1, $2, $3, $4, 'completed')
                RETURNING id, created_at
            """, name, created_by, description, json.dumps(components))

            result = {
                "snapshot_id": row["id"],
                "name": name,
                "created_at": row["created_at"].isoformat(),
                "components": components,
            }
            log.info(f"Snapshot '{name}' erstellt (ID={row['id']})")
            return result

        finally:
            await pool.close()


# ── Snapshots auflisten ───────────────────────────────────────

async def list_snapshots(limit: int = 10) -> list[dict]:
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)
    try:
        rows = await pool.fetch("""
            SELECT id, snapshot_name, created_at, created_by, description,
                   components, status, size_bytes
            FROM knowledge_snapshots
            ORDER BY created_at DESC
            LIMIT $1
        """, limit)
        return [
            {
                "id": r["id"],
                "name": r["snapshot_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "created_by": r["created_by"],
                "description": r["description"],
                "status": r["status"],
                "size_bytes": r["size_bytes"],
                "components": r["components"],
            }
            for r in rows
        ]
    finally:
        await pool.close()


# ── Alte Snapshots aufräumen ──────────────────────────────────

async def cleanup_old_snapshots(keep_last_n: int = KEEP_LAST_N):
    """Löscht Qdrant-Snapshots und PG-Einträge die älter als keep_last_n sind."""
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Snapshots zum Löschen ermitteln
            old_rows = await pool.fetch("""
                SELECT id, snapshot_name, components FROM knowledge_snapshots
                WHERE id NOT IN (
                    SELECT id FROM knowledge_snapshots
                    ORDER BY created_at DESC
                    LIMIT $1
                )
            """, keep_last_n)

            for row in old_rows:
                components = row["components"] or {}
                qdrant_snaps = components.get("qdrant", {}).get("collections", {})

                # Qdrant-Snapshots löschen
                for collection, snap_name in qdrant_snaps.items():
                    await delete_qdrant_snapshot(client, collection, snap_name)

                # PG-Eintrag löschen
                await pool.execute(
                    "DELETE FROM knowledge_snapshots WHERE id = $1", row["id"]
                )
                log.info(f"Alter Snapshot gelöscht: '{row['snapshot_name']}' (ID={row['id']})")

            log.info(f"Cleanup abgeschlossen: {len(old_rows)} alte Snapshot(s) entfernt")
        finally:
            await pool.close()


# ── CLI ───────────────────────────────────────────────────────

async def _auto():
    """Täglicher automatischer Snapshot mit Cleanup."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name  = f"daily-{today}"
    result = await create_snapshot(name, description="Automatischer Tages-Snapshot")
    print(json.dumps(result, indent=2))
    await cleanup_old_snapshots()


async def _list():
    snaps = await list_snapshots(20)
    print(json.dumps(snaps, indent=2, default=str))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if "--auto" in sys.argv:
        asyncio.run(_auto())
    elif "--list" in sys.argv:
        asyncio.run(_list())
    elif "--name" in sys.argv:
        idx = sys.argv.index("--name")
        snap_name = sys.argv[idx + 1]
        desc = sys.argv[sys.argv.index("--desc") + 1] if "--desc" in sys.argv else ""
        asyncio.run(create_snapshot(snap_name, description=desc))
    else:
        print(__doc__)
        sys.exit(1)
