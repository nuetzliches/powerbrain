"""Tests for log_access audit logging with mocked I/O."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import server
from server import log_access


@pytest.fixture(autouse=True)
def _patch_globals(monkeypatch):
    mock_http = AsyncMock()
    mock_pool = AsyncMock()
    monkeypatch.setattr(server, "http", mock_http)
    monkeypatch.setattr(server, "pg_pool", mock_pool)
    return mock_http, mock_pool


class TestLogAccess:
    async def test_inserts_audit_log(self, _patch_globals):
        _, mock_pool = _patch_globals

        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=None)

        mock_pool.execute.assert_called_once()
        call_args = mock_pool.execute.call_args[0]
        assert "agent_access_log" in call_args[0]
        assert call_args[1] == "agent-1"

    async def test_pii_scan_replaces_query(self, _patch_globals):
        mock_http, mock_pool = _patch_globals

        scan_response = MagicMock()
        scan_response.raise_for_status = MagicMock()
        scan_response.json.return_value = {
            "contains_pii": True,
            "masked_text": "<PERSON> braucht Hilfe",
            "entity_types": ["PERSON"],
        }
        mock_http.post.return_value = scan_response

        context = {"query": "Max Mustermann braucht Hilfe"}
        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=context)

        assert context["query"] == "<PERSON> braucht Hilfe"
        assert context["query_contains_pii"] is True

    async def test_pii_scan_failure_does_not_crash(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = Exception("Ingestion down")

        context = {"query": "some query"}
        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=context)

        mock_pool.execute.assert_called_once()

    async def test_no_scan_without_query(self, _patch_globals):
        mock_http, mock_pool = _patch_globals

        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context={"other": "data"})

        mock_http.post.assert_not_called()

    async def test_no_scan_with_none_context(self, _patch_globals):
        mock_http, mock_pool = _patch_globals

        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=None)

        mock_http.post.assert_not_called()
