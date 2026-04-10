"""Repository sync service — orchestrates incremental ingestion from Git repos.

Handles first sync (full tree) and incremental sync (diff since last SHA).
Deleted files are cascade-removed from Qdrant, PostgreSQL, vault, and graph.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import yaml

from ingestion.adapters.base import FileChange
from ingestion.adapters.git_adapter import GitAdapter, RepoConfig

log = logging.getLogger("pb-sync")

REPOS_CONFIG_PATH = Path(__file__).parent / "repos.yaml"


def load_repo_configs(path: Path | None = None) -> list[RepoConfig]:
    """Load repository configurations from YAML."""
    config_path = path or REPOS_CONFIG_PATH
    if not config_path.exists():
        log.warning("No repos.yaml found at %s", config_path)
        return []

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    repos = data.get("repos", [])
    return [RepoConfig(**r) for r in repos]


async def _get_sync_state(
    pool: asyncpg.Pool, repo_name: str
) -> dict[str, Any] | None:
    """Load sync state from PG."""
    row = await pool.fetchrow(
        "SELECT * FROM repo_sync_state WHERE repo_name = $1", repo_name
    )
    return dict(row) if row else None


async def _upsert_sync_state(
    pool: asyncpg.Pool,
    repo_name: str,
    repo_url: str,
    branch: str,
    *,
    last_commit_sha: str | None = None,
    file_count: int = 0,
    status: str = "pending",
    error_message: str | None = None,
) -> None:
    """Create or update sync state."""
    await pool.execute(
        """
        INSERT INTO repo_sync_state
            (repo_name, repo_url, branch, last_commit_sha, last_synced_at,
             file_count, status, error_message, updated_at)
        VALUES ($1, $2, $3, $4, CASE WHEN $6 = 'ok' THEN now() ELSE NULL END,
                $5, $6, $7, now())
        ON CONFLICT (repo_name) DO UPDATE SET
            last_commit_sha = COALESCE(EXCLUDED.last_commit_sha, repo_sync_state.last_commit_sha),
            last_synced_at = CASE WHEN EXCLUDED.status = 'ok' THEN now() ELSE repo_sync_state.last_synced_at END,
            file_count = EXCLUDED.file_count,
            status = EXCLUDED.status,
            error_message = EXCLUDED.error_message,
            updated_at = now()
        """,
        repo_name, repo_url, branch, last_commit_sha, file_count, status, error_message,
    )


async def _ingest_documents(
    http_client: httpx.AsyncClient,
    ingestion_url: str,
    documents: list,
    config: RepoConfig,
) -> int:
    """Ingest normalized documents via the ingestion API. Returns count ingested."""
    ingested = 0
    for doc in documents:
        try:
            resp = await http_client.post(
                f"{ingestion_url}/ingest",
                json={
                    "source": doc.content,
                    "source_type": "github",
                    "collection": config.collection,
                    "project": config.project or config.name,
                    "classification": config.classification,
                    "metadata": {
                        **doc.metadata,
                        "content_type": doc.content_type,
                        "language": doc.language,
                    },
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            ingested += 1
        except Exception:
            log.warning("Failed to ingest %s", doc.source_ref, exc_info=True)
    return ingested


async def _delete_file_documents(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    qdrant_url: str,
    project: str,
    file_path: str,
) -> int:
    """Delete all documents for a specific file path. Returns count deleted."""
    # Find matching document IDs
    rows = await pool.fetch(
        """
        SELECT id FROM documents_meta
        WHERE source_type = 'github'
          AND project = $1
          AND metadata->>'file_path' = $2
        """,
        project, file_path,
    )
    if not rows:
        return 0

    doc_ids = [row["id"] for row in rows]

    # Delete from Qdrant (all collections)
    for collection in ("pb_general", "pb_code", "pb_rules"):
        try:
            await http_client.post(
                f"{qdrant_url}/collections/{collection}/points/delete",
                json={
                    "filter": {
                        "must": [
                            {"key": "source_type", "match": {"value": "github"}},
                            {"key": "project", "match": {"value": project}},
                            {"key": "metadata.file_path", "match": {"value": file_path}},
                        ]
                    }
                },
            )
        except Exception:
            log.warning("Failed to delete from Qdrant/%s for %s", collection, file_path)

    # Delete from PostgreSQL (CASCADE handles vault)
    deleted = await pool.execute(
        "DELETE FROM documents_meta WHERE id = ANY($1::uuid[])",
        doc_ids,
    )

    count = int(deleted.split()[-1]) if deleted else 0
    log.info("Deleted %d document(s) for removed file: %s", count, file_path)
    return count


async def sync_repo(
    config: RepoConfig,
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    ingestion_url: str = "http://ingestion:8081",
    qdrant_url: str = "http://qdrant:6333",
) -> dict[str, Any]:
    """Sync a single repository. Returns a summary dict."""
    t_start = time.time()
    project = config.project or config.name

    log.info("Syncing repo %s (%s, branch=%s)", config.name, config.url, config.branch)

    # Mark as syncing
    await _upsert_sync_state(
        pool, config.name, config.url, config.branch, status="syncing"
    )

    try:
        adapter = GitAdapter(config, http_client)
        current_sha = await adapter.get_current_sha()

        # Check if already synced
        state = await _get_sync_state(pool, config.name)
        last_sha = state["last_commit_sha"] if state else None

        if last_sha == current_sha:
            log.info("Repo %s already at %s, skipping", config.name, current_sha[:8])
            await _upsert_sync_state(
                pool, config.name, config.url, config.branch,
                last_commit_sha=current_sha,
                file_count=state.get("file_count", 0) if state else 0,
                status="ok",
            )
            return {"repo": config.name, "status": "up_to_date", "sha": current_sha}

        ingested = 0
        deleted = 0

        if last_sha is None:
            # First sync: fetch all files
            log.info("Initial sync for %s", config.name)
            docs = await adapter.fetch_all_files()
            ingested = await _ingest_documents(http_client, ingestion_url, docs, config)
        else:
            # Incremental sync
            log.info("Incremental sync for %s: %s..%s", config.name, last_sha[:8], current_sha[:8])

            # Get changes for deletion tracking
            changes = await adapter.get_file_changes(last_sha)

            # Delete removed/modified files first (modified will be re-ingested)
            for change in changes:
                if change.status in ("removed", "modified", "renamed"):
                    path_to_delete = change.previous_path or change.path
                    deleted += await _delete_file_documents(
                        pool, http_client, qdrant_url, project, path_to_delete
                    )

            # Fetch and ingest added/modified files
            docs = await adapter.fetch_changed_files(last_sha)
            ingested = await _ingest_documents(http_client, ingestion_url, docs, config)

        elapsed = time.time() - t_start
        await _upsert_sync_state(
            pool, config.name, config.url, config.branch,
            last_commit_sha=current_sha,
            file_count=(state.get("file_count", 0) if state else 0) + ingested - deleted,
            status="ok",
        )

        result = {
            "repo": config.name,
            "status": "synced",
            "sha": current_sha,
            "ingested": ingested,
            "deleted": deleted,
            "elapsed_seconds": round(elapsed, 1),
        }
        log.info("Sync complete: %s", json.dumps(result))
        return result

    except Exception as e:
        log.exception("Sync failed for %s: %s", config.name, e)
        await _upsert_sync_state(
            pool, config.name, config.url, config.branch,
            status="error",
            error_message=str(e)[:500],
        )
        return {"repo": config.name, "status": "error", "error": str(e)}


async def sync_all_repos(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    ingestion_url: str = "http://ingestion:8081",
    qdrant_url: str = "http://qdrant:6333",
    config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Sync all configured repositories."""
    configs = load_repo_configs(config_path)
    if not configs:
        log.info("No repos configured in repos.yaml")
        return []

    results = []
    for config in configs:
        result = await sync_repo(config, pool, http_client, ingestion_url, qdrant_url)
        results.append(result)

    return results
