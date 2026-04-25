"""Repository sync job — triggers sync for all configured sources.

Calls the ingestion service HTTP endpoint rather than importing
sync logic directly, keeping the worker lightweight.

Covers both Git repositories (repos.yaml) and Office 365 sources
(office365.yaml) via the unified /sync endpoint.
"""

from __future__ import annotations

import logging
import os
import sys

from worker.context import WorkerContext

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config import read_secret  # noqa: E402

log = logging.getLogger("pb-worker.repo-sync")

INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")
INGESTION_AUTH_TOKEN = read_secret("INGESTION_AUTH_TOKEN", "")


def _ingestion_headers() -> dict[str, str]:
    return (
        {"Authorization": f"Bearer {INGESTION_AUTH_TOKEN}"}
        if INGESTION_AUTH_TOKEN
        else {}
    )


async def run(ctx: WorkerContext) -> dict:
    """Trigger repository sync via the ingestion service."""
    try:
        resp = await ctx.http_client.post(
            f"{INGESTION_URL}/sync",
            headers=_ingestion_headers(),
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
