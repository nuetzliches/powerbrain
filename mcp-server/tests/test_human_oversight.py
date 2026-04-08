"""Tests for B-42 Human Oversight (EU AI Act Art. 14).

Covers:
- Circuit breaker state helper + 5s TTL cache
- Dispatch short-circuit when breaker is active
- OPA oversight approval helper (happy path + fail-open)
- Approval-queue interception in _dispatch
- review_pending tool (list/approve/deny)
- get_review_status tool (owner check)
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import server
from server import (
    _CIRCUIT_BREAKER_CACHE,
    _OVERSIGHT_DATA_TOOLS,
    _check_oversight_approval,
    _dispatch,
    _get_circuit_breaker_state,
    _invalidate_circuit_breaker_cache,
)


@pytest.fixture(autouse=True)
def _patch_globals(monkeypatch):
    mock_http = AsyncMock()
    mock_pool = AsyncMock()
    monkeypatch.setattr(server, "http", mock_http)
    monkeypatch.setattr(server, "pg_pool", mock_pool)

    async def _fake_get_pool():
        return mock_pool
    monkeypatch.setattr(server, "get_pg_pool", _fake_get_pool)

    async def _noop_log(*args, **kwargs):
        return None
    monkeypatch.setattr(server, "log_access", _noop_log)

    _invalidate_circuit_breaker_cache()
    return mock_http, mock_pool


def _breaker_row(active=False, reason=None, set_by=None):
    row = MagicMock()
    row.__getitem__ = lambda s, k: {
        "active": active,
        "reason": reason,
        "set_by": set_by,
        "set_at": datetime(2026, 4, 8, 10, 0, 0, tzinfo=timezone.utc),
    }[k]
    return row


# ── Circuit breaker state + cache ──────────────────────────

class TestCircuitBreakerState:
    async def test_inactive_default(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(active=False)
        state = await _get_circuit_breaker_state()
        assert state["active"] is False
        assert state["reason"] is None

    async def test_active_with_reason(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(
            active=True, reason="drill", set_by="admin-1",
        )
        state = await _get_circuit_breaker_state()
        assert state["active"] is True
        assert state["reason"] == "drill"
        assert state["set_by"] == "admin-1"

    async def test_cache_reuse(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(active=False)
        await _get_circuit_breaker_state()
        calls_after_first = mock_pool.fetchrow.call_count
        await _get_circuit_breaker_state()
        assert mock_pool.fetchrow.call_count == calls_after_first

    async def test_db_error_fails_open(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.side_effect = Exception("db down")
        state = await _get_circuit_breaker_state()
        assert state["active"] is False

    async def test_missing_row_fails_open(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = None
        state = await _get_circuit_breaker_state()
        assert state["active"] is False


# ── Circuit breaker short-circuits dispatch ────────────────

class TestCircuitBreakerDispatch:
    async def test_breaker_active_blocks_search(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(
            active=True, reason="maintenance",
        )
        result = await _dispatch(
            "search_knowledge",
            {"query": "test"},
            "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert payload["error"] == "circuit_breaker_active"
        assert payload["reason"] == "maintenance"

    async def test_breaker_active_blocks_all_data_tools(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(active=True)
        for tool in _OVERSIGHT_DATA_TOOLS:
            _invalidate_circuit_breaker_cache()
            result = await _dispatch(tool, {"query": "x"}, "a", "analyst")
            payload = json.loads(result[0].text)
            assert payload["error"] == "circuit_breaker_active"

    async def test_breaker_inactive_allows_non_data_tool(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(active=True)

        # get_system_info must still work even when breaker is on — it is
        # an oversight/operator tool, not a data-retrieval tool.
        async def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"result": {}}
            resp.status_code = 200
            resp.headers = {"content-type": "application/json"}
            return resp
        server.http.get.side_effect = _get
        mock_pool.fetchrow.side_effect = [
            _breaker_row(active=True),  # circuit breaker
            MagicMock(__getitem__=lambda s, k: {"valid": True, "total_checked": 0}[k]),  # audit chain
        ]
        monkey_qdrant = AsyncMock()
        collection_info = MagicMock()
        collection_info.status = "green"
        collection_info.points_count = 0
        collection_info.vectors_count = 0
        monkey_qdrant.get_collection.return_value = collection_info
        server.qdrant = monkey_qdrant

        result = await _dispatch("get_system_info", {}, "any", "analyst")
        payload = json.loads(result[0].text)
        assert "error" not in payload


# ── Oversight approval helper ──────────────────────────────

class TestOversightApproval:
    def _set_opa_responses(self, mock_http, required=True, reason="blocked",
                          timeout=60):
        def _post(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if url.endswith("/requires_approval"):
                resp.json.return_value = {"result": required}
            elif url.endswith("/approval_reason"):
                resp.json.return_value = {"result": reason}
            else:
                resp.json.return_value = {"result": None}
            return resp
        mock_http.post.side_effect = _post

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "pending_review_timeout_minutes" in url:
                resp.json.return_value = {"result": timeout}
            else:
                resp.json.return_value = {"result": None}
            return resp
        mock_http.get.side_effect = _get

    async def test_required_returns_true(self, _patch_globals):
        mock_http, _ = _patch_globals
        self._set_opa_responses(mock_http, required=True, reason="confidential data")

        result = await _check_oversight_approval(
            "confidential", "analyst", "search_knowledge",
        )
        assert result["required"] is True
        assert result["reason"] == "confidential data"
        assert result["timeout_minutes"] == 60

    async def test_not_required_returns_false(self, _patch_globals):
        mock_http, _ = _patch_globals
        self._set_opa_responses(mock_http, required=False, reason="")

        result = await _check_oversight_approval(
            "public", "analyst", "search_knowledge",
        )
        assert result["required"] is False

    async def test_opa_failure_fails_open(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.post.side_effect = Exception("opa down")
        result = await _check_oversight_approval(
            "confidential", "analyst", "search_knowledge",
        )
        # Fail-open: breaker is the backstop
        assert result["required"] is False


# ── Approval queue interception in _dispatch ──────────────

class TestApprovalInterception:
    def _approval_required(self, mock_http):
        async def _post(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if url.endswith("/requires_approval"):
                resp.json.return_value = {"result": True}
            elif url.endswith("/approval_reason"):
                resp.json.return_value = {"result": "needs admin"}
            else:
                resp.json.return_value = {"result": None}
            return resp
        async def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"result": 45}
            return resp
        mock_http.post.side_effect = _post
        mock_http.get.side_effect = _get

    async def test_pending_response_returned(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_pool.fetchrow.side_effect = [
            _breaker_row(active=False),  # circuit breaker
            MagicMock(__getitem__=lambda s, k: {
                "id": "11111111-2222-3333-4444-555555555555",
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=45),
            }[k]),  # insert return
        ]
        self._approval_required(mock_http)

        result = await _dispatch(
            "search_knowledge",
            {"query": "x", "filters": {"classification": "confidential"}},
            "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert payload["status"] == "pending"
        assert payload["review_id"] == "11111111-2222-3333-4444-555555555555"
        assert payload["reason"] == "needs admin"
        assert payload["poll_tool"] == "get_review_status"
        assert payload["timeout_minutes"] == 45

    async def test_admin_bypasses_approval(self, _patch_globals, monkeypatch):
        """Admin dispatch still hits search_knowledge (no approval)."""
        mock_http, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(active=False)
        self._approval_required(mock_http)

        async def _fake_embed(text): return [0.0] * 768
        monkeypatch.setattr(server, "embed_text", _fake_embed)

        mock_qdrant = AsyncMock()
        query_result = MagicMock()
        query_result.points = []
        mock_qdrant.query_points.return_value = query_result
        monkeypatch.setattr(server, "qdrant", mock_qdrant)

        # No-op feedback warning so we do not need to mock pg_pool fetchrow shape
        async def _noop_fb(*args, **kwargs): return None
        monkeypatch.setattr(server, "check_feedback_warning", _noop_fb)

        mock_pool.execute.return_value = "INSERT 0 1"

        result = await _dispatch(
            "search_knowledge",
            {"query": "x", "filters": {"classification": "confidential"}},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        # Not a "pending" payload — admin skipped oversight entirely.
        assert payload.get("status") != "pending"


# ── review_pending tool ────────────────────────────────────

class TestReviewPending:
    async def test_list_requires_admin(self, _patch_globals):
        _, mock_pool = _patch_globals
        result = await _dispatch(
            "review_pending", {"action": "list"}, "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload
        mock_pool.fetch.assert_not_called()

    async def test_list_happy_path(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _breaker_row(active=False)

        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "id": "r-1",
            "agent_id": "a1",
            "agent_role": "analyst",
            "tool": "search_knowledge",
            "arguments": {"query": "foo"},
            "classification": "confidential",
            "status": "pending",
            "reason": "test",
            "created_at": datetime(2026, 4, 8, tzinfo=timezone.utc),
            "expires_at": datetime(2026, 4, 8, 1, tzinfo=timezone.utc),
        }[k]
        mock_pool.fetch.return_value = [row]

        result = await _dispatch(
            "review_pending", {"action": "list", "limit": 10},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["count"] == 1
        assert payload["pending"][0]["review_id"] == "r-1"
        assert payload["pending"][0]["arguments"] == {"query": "foo"}

    async def test_approve_happy_path(self, _patch_globals):
        _, mock_pool = _patch_globals
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "id": "r-1",
            "status": "approved",
            "tool": "search_knowledge",
            "agent_id": "a1",
            "classification": "confidential",
        }[k]
        # review_pending is NOT in _OVERSIGHT_DATA_TOOLS — circuit breaker
        # is not consulted, so the only fetchrow call is the UPDATE RETURNING.
        mock_pool.fetchrow.return_value = row

        result = await _dispatch(
            "review_pending",
            {"action": "approve", "review_id": "r-1", "reason": "ok"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["status"] == "approved"
        assert payload["review_id"] == "r-1"

    async def test_approve_missing_review_id(self, _patch_globals):
        result = await _dispatch(
            "review_pending", {"action": "approve"}, "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload

    async def test_approve_not_found(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = None
        result = await _dispatch(
            "review_pending",
            {"action": "deny", "review_id": "does-not-exist"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload


# ── get_review_status tool ─────────────────────────────────

class TestGetReviewStatus:
    async def test_missing_review_id(self, _patch_globals):
        result = await _dispatch(
            "get_review_status", {}, "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload

    async def test_not_found(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = None
        result = await _dispatch(
            "get_review_status", {"review_id": "missing"},
            "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload

    async def test_owner_can_read(self, _patch_globals):
        _, mock_pool = _patch_globals
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "id": "r-1",
            "agent_id": "agent-1",
            "agent_role": "analyst",
            "tool": "search_knowledge",
            "arguments": {"q": "x"},
            "classification": "confidential",
            "status": "approved",
            "reason": "ok",
            "decision_by": "admin-1",
            "decision_at": datetime(2026, 4, 8, tzinfo=timezone.utc),
            "created_at": datetime(2026, 4, 8, tzinfo=timezone.utc),
            "expires_at": datetime(2026, 4, 8, 1, tzinfo=timezone.utc),
        }[k]
        mock_pool.fetchrow.return_value = row

        result = await _dispatch(
            "get_review_status", {"review_id": "r-1"},
            "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert payload["status"] == "approved"
        assert payload["review_id"] == "r-1"

    async def test_non_owner_denied(self, _patch_globals):
        _, mock_pool = _patch_globals
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "id": "r-1",
            "agent_id": "other-agent",
            "agent_role": "analyst",
            "tool": "search_knowledge",
            "arguments": {},
            "classification": "confidential",
            "status": "pending",
            "reason": "",
            "decision_by": None,
            "decision_at": None,
            "created_at": datetime(2026, 4, 8, tzinfo=timezone.utc),
            "expires_at": datetime(2026, 4, 8, 1, tzinfo=timezone.utc),
        }[k]
        mock_pool.fetchrow.return_value = row

        result = await _dispatch(
            "get_review_status", {"review_id": "r-1"},
            "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload

    async def test_admin_can_read_any(self, _patch_globals):
        _, mock_pool = _patch_globals
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "id": "r-1",
            "agent_id": "other-agent",
            "agent_role": "analyst",
            "tool": "search_knowledge",
            "arguments": {},
            "classification": "confidential",
            "status": "pending",
            "reason": "",
            "decision_by": None,
            "decision_at": None,
            "created_at": datetime(2026, 4, 8, tzinfo=timezone.utc),
            "expires_at": datetime(2026, 4, 8, 1, tzinfo=timezone.utc),
        }[k]
        mock_pool.fetchrow.return_value = row

        result = await _dispatch(
            "get_review_status", {"review_id": "r-1"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["review_id"] == "r-1"
