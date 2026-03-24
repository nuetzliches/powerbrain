"""
Proxy API-key authentication.
Verifies kb_ API keys against the shared api_keys PostgreSQL table.
"""

import hashlib
import logging
import time
from typing import Any

import asyncpg

import config

log = logging.getLogger("pb-proxy.auth")

# Result type for verified keys
VerifiedKey = dict[str, str]  # {"agent_id": ..., "agent_role": ...}


class ProxyKeyVerifier:
    """Verifies API keys against PostgreSQL with in-memory caching."""

    def __init__(self, cache_ttl: int = 60) -> None:
        self._pool: asyncpg.Pool | None = None
        self._cache: dict[str, tuple[VerifiedKey | None, float]] = {}
        self._cache_ttl = cache_ttl

    async def start(self) -> None:
        """Create the connection pool."""
        self._pool = await asyncpg.create_pool(
            host=config.PG_HOST,
            port=config.PG_PORT,
            database=config.PG_DATABASE,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            min_size=1,
            max_size=5,
        )
        log.info("ProxyKeyVerifier connected to PostgreSQL")

    async def stop(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            log.info("ProxyKeyVerifier disconnected from PostgreSQL")

    async def verify(self, token: str) -> VerifiedKey | None:
        """Verify an API key. Returns {"agent_id": ..., "agent_role": ...} or None.

        - Rejects empty tokens and non-kb_ prefixed tokens immediately
        - Uses in-memory cache with TTL
        - Updates last_used_at (throttled, fire-and-forget)
        """
        if not token or not token.startswith("kb_"):
            return None

        # Check cache
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(key_hash)
        if cached is not None:
            result, timestamp = cached
            if time.monotonic() - timestamp < self._cache_ttl:
                return result

        # DB lookup
        if self._pool is None:
            log.error("ProxyKeyVerifier not started (no pool)")
            return None

        row = await self._pool.fetchrow(
            "SELECT agent_id, agent_role FROM api_keys "
            "WHERE key_hash = $1 AND active = true "
            "AND (expires_at IS NULL OR expires_at > now())",
            key_hash,
        )

        if row is None:
            self._cache[key_hash] = (None, time.monotonic())
            return None

        result: VerifiedKey = {
            "agent_id": row["agent_id"],
            "agent_role": row["agent_role"],
        }
        self._cache[key_hash] = (result, time.monotonic())

        # Update last_used_at (fire-and-forget, throttled)
        try:
            await self._pool.execute(
                "UPDATE api_keys SET last_used_at = now() "
                "WHERE key_hash = $1 AND (last_used_at IS NULL "
                "OR last_used_at < now() - interval '5 minutes')",
                key_hash,
            )
        except Exception:
            pass  # Non-critical

        return result

    def invalidate_cache(self) -> None:
        """Clear the entire cache (e.g., after downstream 401)."""
        self._cache.clear()
