"""
Snapshot Service (Knowledge Versioning)
======================================================
Creates and manages snapshots of the knowledge base:
  - Qdrant: native snapshot API for all collections
  - PostgreSQL: row counts + metadata
  - OPA: current policy commit hash from Forgejo

Can be used as a module (called by the ingestion API) or directly via CLI:

  python snapshot_service.py --auto          # daily snapshot + cleanup
  python snapshot_service.py --list          # list snapshots
  python snapshot_service.py --name my-snap  # create a named snapshot
"""

import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timezone

import httpx
import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.config import read_secret, build_postgres_url

log = logging.getLogger("pb-snapshot")

# ── Configuration ────────────────────────────────────────────
QDRANT_URL   = os.getenv("QDRANT_URL",   "http://qdrant:6333")
POSTGRES_URL = build_postgres_url()
FORGEJO_URL  = os.getenv("FORGEJO_URL",  "http://forgejo.local:3000")
FORGEJO_TOKEN = read_secret("FORGEJO_TOKEN")

QDRANT_COLLECTIONS = ["pb_general", "pb_code", "pb_rules"]
PG_SNAPSHOT_TABLES = ["datasets", "dataset_rows", "documents_meta"]
KEEP_LAST_N        = int(os.getenv("SNAPSHOT_KEEP_LAST_N", "10"))


# ── Qdrant Snapshots ──────────────────────────────────────────

async def create_qdrant_snapshots(client: httpx.AsyncClient) -> dict:
    """Creates Qdrant snapshots for all collections. Returns {collection: snapshot_name}."""
    snapshots = {}
    for collection in QDRANT_COLLECTIONS:
        try:
            resp = await client.post(f"{QDRANT_URL}/collections/{collection}/snapshots")
            if resp.status_code == 404:
                log.warning(f"Collection '{collection}' not found, skipping")
                continue
            resp.raise_for_status()
            snap_name = resp.json()["result"]["name"]
            snapshots[collection] = snap_name
            log.info(f"Qdrant snapshot created: {collection} → {snap_name}")
        except Exception as e:
            log.error(f"Qdrant snapshot for '{collection}' failed: {e}")
    return snapshots


async def list_qdrant_snapshots(client: httpx.AsyncClient, collection: str) -> list[dict]:
    """Lists existing Qdrant snapshots for a collection."""
    try:
        resp = await client.get(f"{QDRANT_URL}/collections/{collection}/snapshots")
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        log.error(f"Listing Qdrant snapshots for '{collection}' failed: {e}")
        return []


async def delete_qdrant_snapshot(client: httpx.AsyncClient, collection: str, snapshot_name: str):
    """Deletes a Qdrant snapshot."""
    try:
        resp = await client.delete(
            f"{QDRANT_URL}/collections/{collection}/snapshots/{snapshot_name}"
        )
        resp.raise_for_status()
        log.info(f"Qdrant snapshot deleted: {collection}/{snapshot_name}")
    except Exception as e:
        log.error(f"Qdrant snapshot deletion failed: {e}")


async def restore_qdrant_snapshot(client: httpx.AsyncClient, collection: str, snapshot_name: str):
    """Restores a Qdrant collection from a snapshot."""
    resp = await client.put(
        f"{QDRANT_URL}/collections/{collection}/snapshots/recover",
        json={"location": f"file:///qdrant/snapshots/{collection}/{snapshot_name}"}
    )
    resp.raise_for_status()
    log.info(f"Qdrant collection '{collection}' restored from snapshot '{snapshot_name}'")


# ── PostgreSQL Row-Counts ─────────────────────────────────────

async def get_pg_row_counts(pool: asyncpg.Pool) -> dict:
    """Returns current row counts of the relevant tables."""
    counts = {}
    for table in PG_SNAPSHOT_TABLES:
        try:
            row = await pool.fetchrow(f"SELECT COUNT(*) AS cnt FROM {table}")
            counts[table] = row["cnt"] if row else 0
        except Exception:
            counts[table] = -1  # Table does not (yet) exist
    return counts


# ── OPA Policy Commit ─────────────────────────────────────────

async def get_policy_commit(client: httpx.AsyncClient) -> str | None:
    """Gets the current commit hash of the policy repo from Forgejo."""
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
        log.warning(f"Policy commit not retrievable: {e}")
        return None


# ── Create Snapshot ───────────────────────────────────────────

async def create_snapshot(name: str, description: str = "",
                          created_by: str = "system") -> dict:
    """
    Creates a complete knowledge snapshot:
    1. Qdrant snapshots for all collections
    2. PostgreSQL row counts + timestamp
    3. OPA policy commit hash

    Stores metadata in `knowledge_snapshots`.
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
            log.info(f"Snapshot '{name}' created (ID={row['id']})")
            return result

        finally:
            await pool.close()


# ── List Snapshots ────────────────────────────────────────────

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


# ── Clean Up Old Snapshots ────────────────────────────────────

async def cleanup_old_snapshots(keep_last_n: int = KEEP_LAST_N):
    """Deletes Qdrant snapshots and PG entries older than keep_last_n."""
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Determine snapshots to delete
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

                # Delete Qdrant snapshots
                for collection, snap_name in qdrant_snaps.items():
                    await delete_qdrant_snapshot(client, collection, snap_name)

                # Delete PG entry
                await pool.execute(
                    "DELETE FROM knowledge_snapshots WHERE id = $1", row["id"]
                )
                log.info(f"Old snapshot deleted: '{row['snapshot_name']}' (ID={row['id']})")

            log.info(f"Cleanup completed: {len(old_rows)} old snapshot(s) removed")
        finally:
            await pool.close()


# ── CLI ───────────────────────────────────────────────────────

async def _auto():
    """Daily automatic snapshot with cleanup."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name  = f"daily-{today}"
    result = await create_snapshot(name, description="Automatic daily snapshot")
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
