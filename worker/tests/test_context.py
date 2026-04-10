"""Tests for worker.context — WorkerContext creation and cleanup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.context import WorkerContext


class TestWorkerContextCreate:
    @pytest.fixture(autouse=True)
    def _patch_deps(self, monkeypatch):
        self.mock_pool = AsyncMock()
        self.mock_pool.fetchval.return_value = 1

        async def _fake_create_pool(*args, **kwargs):
            self.pool_args = args
            self.pool_kwargs = kwargs
            return self.mock_pool

        monkeypatch.setattr("worker.context.asyncpg.create_pool", _fake_create_pool)

    async def test_create_defaults(self):
        ctx = await WorkerContext.create()
        assert ctx.opa_url == "http://opa:8181"
        assert ctx.qdrant_url == "http://qdrant:6333"
        assert ctx.audit_retention_days == 365
        assert ctx.pending_review_grace_minutes == 0
        assert ctx.pg_pool is self.mock_pool

    async def test_create_custom_env(self, monkeypatch):
        monkeypatch.setenv("OPA_URL", "http://custom-opa:9999")
        monkeypatch.setenv("QDRANT_URL", "http://custom-qdrant:7777")
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
        monkeypatch.setenv("PENDING_REVIEW_GRACE_MINUTES", "15")

        ctx = await WorkerContext.create()
        assert ctx.opa_url == "http://custom-opa:9999"
        assert ctx.qdrant_url == "http://custom-qdrant:7777"
        assert ctx.audit_retention_days == 30
        assert ctx.pending_review_grace_minutes == 15

    async def test_create_verifies_connection(self):
        await WorkerContext.create()
        self.mock_pool.fetchval.assert_called_once_with("SELECT 1")

    async def test_create_pool_args(self, monkeypatch):
        monkeypatch.setattr("shared.config.PG_POOL_MIN", 3)
        monkeypatch.setattr("shared.config.PG_POOL_MAX", 7)

        await WorkerContext.create()
        assert self.pool_kwargs["min_size"] == 3
        assert self.pool_kwargs["max_size"] == 7


class TestWorkerContextClose:
    async def test_close_happy_path(self):
        http = AsyncMock()
        pool = AsyncMock()
        ctx = WorkerContext(
            pg_pool=pool, http_client=http,
            opa_url="x", qdrant_url="y",
        )
        await ctx.close()
        http.aclose.assert_called_once()
        pool.close.assert_called_once()

    async def test_close_swallows_http_error(self):
        http = AsyncMock()
        http.aclose.side_effect = RuntimeError("connection reset")
        pool = AsyncMock()
        ctx = WorkerContext(
            pg_pool=pool, http_client=http,
            opa_url="x", qdrant_url="y",
        )
        # Must not raise
        await ctx.close()
        pool.close.assert_called_once()

    async def test_close_swallows_pool_error(self):
        http = AsyncMock()
        pool = AsyncMock()
        pool.close.side_effect = RuntimeError("pool broken")
        ctx = WorkerContext(
            pg_pool=pool, http_client=http,
            opa_url="x", qdrant_url="y",
        )
        # Must not raise
        await ctx.close()
        http.aclose.assert_called_once()
