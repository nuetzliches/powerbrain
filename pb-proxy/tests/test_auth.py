"""Tests for ProxyKeyVerifier."""

import hashlib
import pytest
from unittest.mock import AsyncMock


@pytest.fixture
def mock_pool():
    """Create a mock asyncpg connection pool."""
    pool = AsyncMock()
    return pool


@pytest.fixture
def verifier(mock_pool):
    """Create a ProxyKeyVerifier with mocked pool."""
    from auth import ProxyKeyVerifier
    v = ProxyKeyVerifier.__new__(ProxyKeyVerifier)
    v._pool = mock_pool
    v._cache = {}
    v._cache_ttl = 60
    v._max_cache_size = 10_000
    return v


@pytest.mark.asyncio
async def test_verify_valid_key(verifier, mock_pool):
    """Valid key returns agent_id and agent_role."""
    key = "kb_test_valid_key_12345678901234567890"
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    mock_pool.fetchrow.return_value = {
        "agent_id": "test-agent",
        "agent_role": "developer",
    }

    result = await verifier.verify(key)

    assert result is not None
    assert result["agent_id"] == "test-agent"
    assert result["agent_role"] == "developer"
    mock_pool.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_verify_invalid_key(verifier, mock_pool):
    """Invalid key returns None."""
    mock_pool.fetchrow.return_value = None

    result = await verifier.verify("kb_invalid_key_does_not_exist")

    assert result is None


@pytest.mark.asyncio
async def test_verify_empty_key(verifier):
    """Empty key returns None without DB call."""
    result = await verifier.verify("")
    assert result is None


@pytest.mark.asyncio
async def test_verify_cached(verifier, mock_pool):
    """Second call uses cache, not DB."""
    key = "kb_cached_key_123456789012345678901234"
    mock_pool.fetchrow.return_value = {
        "agent_id": "cached-agent",
        "agent_role": "admin",
    }

    result1 = await verifier.verify(key)
    result2 = await verifier.verify(key)

    assert result1 == result2
    assert mock_pool.fetchrow.call_count == 1  # Only one DB call


@pytest.mark.asyncio
async def test_verify_non_kb_prefix(verifier):
    """Non-kb_ prefixed tokens are rejected immediately."""
    result = await verifier.verify("sk-ant-some-anthropic-key")
    assert result is None
