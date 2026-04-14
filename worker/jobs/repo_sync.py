"""Repository sync job — triggers sync for all configured sources.

Calls the ingestion service HTTP endpoint rather than importing
sync logic directly, keeping the worker lightweight.

Covers both Git repositories (repos.yaml) and Office 365 sources
(office365.yaml) via the unified /sync endpoint.
"""

from __future__ import annotations

import logging
import os

from worker.context import WorkerContext

log = logging.getLogger("pb-worker.repo-sync")

INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")


async def run(ctx: WorkerContext) -> dict:
    """Trigger repository sync via the ingestion service."""
    try:
        resp = await ctx.http_client.post(
            f"{INGESTION_URL}/sync",
            timeout=600.0,  # 10min — Office 365 syncs can be slower than Git
        )
        resp.raise_for_status()
        result = resp.json()
        repos = result.get("repos", [])
        synced = sum(1 for r in repos if r.get("status") == "synced")
        errors = sum(1 for r in repos if r.get("status") == "error")
        return {"repos": len(repos), "synced": synced, "errors": errors}
    except Exception as e:
        log.warning("Repo sync trigger failed: %s", e)
        return {"error": str(e)}
