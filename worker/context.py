"""Shared runtime context for worker jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import asyncpg
import httpx


@dataclass
class WorkerContext:
    """Holds resources reused across jobs and a clean shutdown handle."""
    pg_pool:        asyncpg.Pool
    http_client:    httpx.AsyncClient
    opa_url:        str
    qdrant_url:     str
    audit_retention_days: int = 365
    pending_review_grace_minutes: int = 0
    extra:          dict = field(default_factory=dict)

    async def close(self) -> None:
        try:
            await self.http_client.aclose()
        except Exception:
            pass
        try:
            await self.pg_pool.close()
        except Exception:
            pass

    @classmethod
    async def create(cls) -> "WorkerContext":
        from shared.config import build_postgres_url, PG_POOL_MIN, PG_POOL_MAX
        pool = await asyncpg.create_pool(
            build_postgres_url(),
            min_size=PG_POOL_MIN,
            max_size=PG_POOL_MAX,
        )
        await pool.fetchval("SELECT 1")
        return cls(
            pg_pool=pool,
            http_client=httpx.AsyncClient(timeout=10.0),
            opa_url=os.getenv("OPA_URL", "http://opa:8181"),
            qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333"),
            audit_retention_days=int(os.getenv("AUDIT_RETENTION_DAYS", "365")),
            pending_review_grace_minutes=int(os.getenv("PENDING_REVIEW_GRACE_MINUTES", "0")),
        )
