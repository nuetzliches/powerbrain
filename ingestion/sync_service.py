"""Repository sync service — orchestrates incremental ingestion from Git repos.

Handles first sync (full tree) and incremental sync (diff since last SHA).
Deleted files are cascade-removed from Qdrant, PostgreSQL, vault, and graph.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import yaml

from ingestion.adapters.base import FileChange
from ingestion.adapters.git_adapter import GitAdapter, RepoConfig

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.config import read_secret  # noqa: E402

log = logging.getLogger("pb-sync")

REPOS_CONFIG_PATH = Path(__file__).parent / "repos.yaml"
O365_CONFIG_PATH = Path(__file__).parent / "office365.yaml"

# B-50: loopback bearer for self-calls into the ingestion API. The
# /sync orchestrator runs inside the ingestion container and POSTs
# back to its own /ingest endpoint over HTTP, so it must speak the
# same service-token middleware as remote callers.
_INGESTION_AUTH_TOKEN = read_secret("INGESTION_AUTH_TOKEN", "")


def _loopback_headers() -> dict[str, str]:
    return (
        {"Authorization": f"Bearer {_INGESTION_AUTH_TOKEN}"}
        if _INGESTION_AUTH_TOKEN
        else {}
    )


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


def load_office365_configs(
    path: Path | None = None,
) -> list:
    """Load Office 365 source configurations from YAML.

    Returns list of Office365Config instances. Resolves client_secret
    from Docker Secrets.
    """
    config_path = path or O365_CONFIG_PATH
    if not config_path.exists():
        log.debug("No office365.yaml found at %s", config_path)
        return []

    try:
        from ingestion.adapters.office365.adapter import Office365Config
        from shared.config import read_secret
    except ImportError:
        log.warning("Office 365 adapter not available (missing dependencies)")
        return []

    with open(config_path) as f:
        data = yaml.safe_load(f) or {}

    defaults = data.get("defaults", {})
    sources = data.get("sources", [])
    configs = []

    for src in sources:
        # Apply defaults
        for key, val in defaults.items():
            src.setdefault(key, val)

        # Resolve client_secret from Docker Secret
        secret_name = src.pop("client_secret_env", "AZURE_CLIENT_SECRET")
        src.setdefault("client_secret", read_secret(secret_name, ""))

        # Resolve refresh_token for OneNote delegated auth
        if src.get("onenote"):
            rt_env = src.pop("refresh_token_env", "AZURE_ONENOTE_REFRESH_TOKEN")
            src.setdefault("refresh_token", read_secret(rt_env, ""))

        configs.append(Office365Config(**src))

    return configs


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
                headers=_loopback_headers(),
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


async def sync_office365(
    config,  # Office365Config
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    ingestion_url: str = "http://ingestion:8081",
    qdrant_url: str = "http://qdrant:6333",
) -> dict[str, Any]:
    """Sync a single Office 365 source. Returns a summary dict."""
    from ingestion.adapters.office365.adapter import Office365Adapter

    t_start = time.time()
    project = config.project or config.name

    log.info("Syncing Office 365 source %s (tenant=%s)", config.name, config.tenant_id)

    await _upsert_sync_state(
        pool, config.name, f"office365://{config.tenant_id}", "main",
        status="syncing",
    )

    try:
        adapter = Office365Adapter(config, http_client)

        # Load existing delta links from sync state
        state = await _get_sync_state(pool, config.name)
        if state and state.get("delta_links"):
            adapter.delta_links = state["delta_links"]

        last_sha = state["last_commit_sha"] if state else None

        ingested = 0
        deleted = 0

        if last_sha is None:
            # Initial sync
            log.info("Initial Office 365 sync for %s", config.name)
            docs = await adapter.fetch_all_files()
            ingested = await _ingest_o365_documents(
                http_client, ingestion_url, docs, config
            )
        else:
            # Incremental sync
            log.info("Incremental Office 365 sync for %s", config.name)

            # Get changes for deletion tracking
            changes = await adapter.get_file_changes(last_sha)

            # Delete removed items
            for change in changes:
                if change.status == "removed":
                    deleted += await _delete_o365_documents(
                        pool, http_client, qdrant_url, project, change.path, config.name,
                    )

            # Fetch and ingest changed content
            docs = await adapter.fetch_changed_files(last_sha)
            ingested = await _ingest_o365_documents(
                http_client, ingestion_url, docs, config
            )

        elapsed = time.time() - t_start
        current_sha = await adapter.get_current_sha()

        # Save delta links for next sync
        await _upsert_sync_state(
            pool, config.name, f"office365://{config.tenant_id}", "main",
            last_commit_sha=current_sha,
            file_count=(state.get("file_count", 0) if state else 0) + ingested - deleted,
            status="ok",
        )
        # Store delta links in the new column
        await pool.execute(
            """UPDATE repo_sync_state
               SET delta_links = $2::jsonb, source_type = 'office365'
               WHERE repo_name = $1""",
            config.name, json.dumps(adapter.delta_links),
        )

        result = {
            "source": config.name,
            "type": "office365",
            "status": "synced",
            "ingested": ingested,
            "deleted": deleted,
            "delta_links": len(adapter.delta_links),
            "elapsed_seconds": round(elapsed, 1),
        }
        log.info("Office 365 sync complete: %s", json.dumps(result))
        return result

    except Exception as e:
        log.exception("Office 365 sync failed for %s: %s", config.name, e)
        await _upsert_sync_state(
            pool, config.name, f"office365://{config.tenant_id}", "main",
            status="error", error_message=str(e)[:500],
        )
        return {"source": config.name, "type": "office365", "status": "error", "error": str(e)}


async def _ingest_o365_documents(
    http_client: httpx.AsyncClient,
    ingestion_url: str,
    documents: list,
    config,  # Office365Config
) -> int:
    """Ingest Office 365 documents via the ingestion API."""
    ingested = 0
    for doc in documents:
        try:
            # Determine classification from source_type-specific configs
            classification = _resolve_o365_classification(doc, config)

            resp = await http_client.post(
                f"{ingestion_url}/ingest",
                json={
                    "source": doc.content,
                    "source_type": doc.source_type,
                    "collection": config.collection,
                    "project": config.project or config.name,
                    "classification": classification,
                    "metadata": {
                        **doc.metadata,
                        "content_type": doc.content_type,
                        "language": doc.language,
                    },
                },
                headers=_loopback_headers(),
                timeout=120.0,
            )
            resp.raise_for_status()
            ingested += 1
        except Exception:
            log.warning("Failed to ingest %s", doc.source_ref, exc_info=True)
    return ingested


def _resolve_o365_classification(doc, config) -> str:
    """Resolve classification for an Office 365 document.

    Checks source_type-specific configs (site → classification, mailbox → classification, etc.)
    Falls back to 'internal'.
    """
    metadata = doc.metadata

    # SharePoint: match by site_url
    if doc.source_type == "office365":
        site_url = metadata.get("site_url", "")
        for site_cfg in config.sites:
            if site_cfg.get("url", "") == site_url:
                return site_cfg.get("classification", "internal")

    # Email: match by mailbox
    if doc.source_type == "email":
        mailbox = metadata.get("mailbox", "")
        for mb_cfg in config.mailboxes:
            if mb_cfg.get("user", "") == mailbox:
                return mb_cfg.get("classification", "internal")

    # Teams: match by team name
    if doc.source_type == "teams":
        team = metadata.get("team", "")
        for team_cfg in config.teams:
            if team_cfg.get("name", "") == team:
                return team_cfg.get("classification", "internal")

    # OneNote: match by notebook name
    if doc.source_type == "onenote":
        notebook = metadata.get("notebook", "")
        for on_cfg in config.onenote:
            if on_cfg.get("notebook", "") == notebook:
                return on_cfg.get("classification", "internal")

    return "internal"


async def _delete_o365_documents(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    qdrant_url: str,
    project: str,
    file_path: str,
    source_name: str,
) -> int:
    """Delete Office 365 documents matching a file path."""
    # Find matching document IDs across all O365 source types
    rows = await pool.fetch(
        """
        SELECT id FROM documents_meta
        WHERE source_type IN ('office365', 'email', 'teams', 'onenote')
          AND project = $1
          AND metadata->>'file_path' = $2
        """,
        project, file_path,
    )
    if not rows:
        # Try matching by source_ref pattern
        rows = await pool.fetch(
            """
            SELECT id FROM documents_meta
            WHERE source_type IN ('office365', 'email', 'teams', 'onenote')
              AND project = $1
              AND source_ref LIKE $2
            """,
            project, f"%{file_path}%",
        )
    if not rows:
        return 0

    doc_ids = [row["id"] for row in rows]

    # Delete from Qdrant
    for collection in ("pb_general", "pb_code", "pb_rules"):
        try:
            await http_client.post(
                f"{qdrant_url}/collections/{collection}/points/delete",
                json={
                    "filter": {
                        "must": [
                            {"key": "project", "match": {"value": project}},
                            {"key": "source_ref", "match": {"any": [
                                f for f in [f"office365:{source_name}:{file_path}"]
                            ]}},
                        ]
                    }
                },
            )
        except Exception:
            log.warning("Failed to delete from Qdrant/%s for %s", collection, file_path)

    # Delete from PostgreSQL (CASCADE handles vault)
    deleted = await pool.execute(
        "DELETE FROM documents_meta WHERE id = ANY($1::uuid[])", doc_ids,
    )
    count = int(deleted.split()[-1]) if deleted else 0
    log.info("Deleted %d Office 365 document(s) for: %s", count, file_path)
    return count


async def sync_all_repos(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    ingestion_url: str = "http://ingestion:8081",
    qdrant_url: str = "http://qdrant:6333",
    config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Sync all configured repositories and Office 365 sources."""
    results = []

    # Git repositories
    configs = load_repo_configs(config_path)
    if not configs:
        log.info("No repos configured in repos.yaml")
    else:
        for config in configs:
            result = await sync_repo(config, pool, http_client, ingestion_url, qdrant_url)
            results.append(result)

    # Office 365 sources
    o365_configs = load_office365_configs()
    if o365_configs:
        for o365_config in o365_configs:
            result = await sync_office365(
                o365_config, pool, http_client, ingestion_url, qdrant_url,
            )
            results.append(result)

    if not results:
        log.info("No sources configured (repos.yaml or office365.yaml)")

    return results
