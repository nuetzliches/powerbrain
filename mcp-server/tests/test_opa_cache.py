"""Tests for OPA access policy result caching."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestOpaCache:
    """Test OPA result caching in check_opa_policy()."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """Clear the OPA cache before each test."""
        import server
        if hasattr(server, '_opa_cache'):
            server._opa_cache.clear()
        yield

    @pytest.fixture
    def mock_http(self):
        """Mock the module-level HTTP client."""
        mock = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"result": True}
        mock.post.return_value = resp
        return mock

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self, mock_http):
        """Same (role, classification, action) should hit cache on second call."""
        import server
        server.OPA_CACHE_ENABLED = True
        with patch.object(server, 'http', mock_http):
            # First call — hits OPA
            r1 = await server.check_opa_policy("agent1", "analyst", "res/1", "internal")
            # Second call — same role+classification+action, should use cache
            r2 = await server.check_opa_policy("agent2", "analyst", "res/2", "internal")

            assert r1["allowed"] is True
            assert r2["allowed"] is True
            # OPA HTTP should only be called once
            assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_different_classification_misses_cache(self, mock_http):
        """Different classification should be a cache miss."""
        import server
        server.OPA_CACHE_ENABLED = True
        with patch.object(server, 'http', mock_http):
            await server.check_opa_policy("a1", "analyst", "r/1", "internal")
            await server.check_opa_policy("a1", "analyst", "r/2", "confidential")

            assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_disabled(self, mock_http):
        """When cache is disabled, every call hits OPA."""
        import server
        server.OPA_CACHE_ENABLED = False
        with patch.object(server, 'http', mock_http):
            await server.check_opa_policy("a1", "analyst", "r/1", "internal")
            await server.check_opa_policy("a1", "analyst", "r/2", "internal")

            assert mock_http.post.call_count == 2
