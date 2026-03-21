"""Tests for ApiKeyVerifier with mocked PostgreSQL."""

import hashlib
from unittest.mock import AsyncMock, patch
import pytest

import server
from server import ApiKeyVerifier


@pytest.fixture
def verifier():
    return ApiKeyVerifier()


@pytest.fixture
def mock_pool(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setattr(server, "pg_pool", pool)
    return pool


class TestApiKeyVerifier:
    async def test_valid_key_returns_access_token(self, verifier, mock_pool):
        key_hash = hashlib.sha256("kb_test_key".encode()).hexdigest()
        mock_pool.fetchrow.return_value = {
            "agent_id": "agent-1",
            "agent_role": "analyst",
        }
        mock_pool.execute.return_value = None

        result = await verifier.verify_token("kb_test_key")

        assert result is not None
        assert result.client_id == "agent-1"
        assert result.scopes == ["analyst"]
        # Verify the correct hash was used in the query
        call_args = mock_pool.fetchrow.call_args
        assert key_hash == call_args[0][1]

    async def test_invalid_key_returns_none(self, verifier, mock_pool):
        mock_pool.fetchrow.return_value = None

        result = await verifier.verify_token("kb_invalid_key")
        assert result is None

    async def test_empty_token_returns_none(self, verifier, mock_pool):
        result = await verifier.verify_token("")
        assert result is None
        mock_pool.fetchrow.assert_not_called()

    async def test_last_used_update_failure_does_not_break_auth(self, verifier, mock_pool):
        mock_pool.fetchrow.return_value = {
            "agent_id": "agent-1",
            "agent_role": "developer",
        }
        mock_pool.execute.side_effect = Exception("DB write failed")

        result = await verifier.verify_token("kb_test_key")
        assert result is not None
        assert result.client_id == "agent-1"

    async def test_token_is_preserved_in_access_token(self, verifier, mock_pool):
        mock_pool.fetchrow.return_value = {
            "agent_id": "agent-2",
            "agent_role": "admin",
        }
        mock_pool.execute.return_value = None

        result = await verifier.verify_token("kb_my_token")
        assert result.token == "kb_my_token"
