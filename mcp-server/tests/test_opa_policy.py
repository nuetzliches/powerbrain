"""Tests for OPA policy checking and filtering."""

from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
import pytest

import server
from server import check_opa_policy, filter_by_policy


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


class TestCheckOpaPolicy:
    async def test_allow(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        result = await check_opa_policy("agent-1", "analyst", "search", "public")
        assert result["allowed"] is True
        assert result["input"]["agent_role"] == "analyst"

    async def test_deny(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": False}
        _patch_http.post.return_value = response

        result = await check_opa_policy("agent-1", "analyst", "search", "restricted")
        assert result["allowed"] is False

    async def test_fail_closed_on_error(self, _patch_http):
        """Non-retryable errors should deny access (fail-closed)."""
        _patch_http.post.side_effect = Exception("OPA unreachable")

        result = await check_opa_policy("agent-1", "analyst", "search", "internal")
        assert result["allowed"] is False

    async def test_default_action_is_read(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        result = await check_opa_policy("agent-1", "admin", "resource", "public")
        assert result["input"]["action"] == "read"

    async def test_custom_action(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        result = await check_opa_policy("agent-1", "admin", "resource", "public", action="write")
        assert result["input"]["action"] == "write"

    async def test_calls_correct_opa_endpoint(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        await check_opa_policy("agent-1", "analyst", "res", "public")

        call_args = _patch_http.post.call_args
        assert "/v1/data/pb/access/allow" in call_args[0][0]


def _make_hit(hit_id, classification="internal"):
    """Create a mock Qdrant ScoredPoint."""
    hit = SimpleNamespace()
    hit.id = hit_id
    hit.payload = {"classification": classification}
    return hit


class TestFilterByPolicy:
    async def test_filters_denied_hits(self, _patch_http):
        allow_resp = MagicMock()
        allow_resp.raise_for_status = MagicMock()
        allow_resp.json.return_value = {"result": True}

        deny_resp = MagicMock()
        deny_resp.raise_for_status = MagicMock()
        deny_resp.json.return_value = {"result": False}

        _patch_http.post.side_effect = [allow_resp, deny_resp]

        hits = [_make_hit("doc-1", "public"), _make_hit("doc-2", "restricted")]
        result = await filter_by_policy(hits, "agent-1", "analyst", "search")

        assert len(result) == 1
        assert result[0].id == "doc-1"

    async def test_empty_hits_returns_empty(self, _patch_http):
        result = await filter_by_policy([], "agent-1", "analyst", "search")
        assert result == []
        _patch_http.post.assert_not_called()

    async def test_all_allowed(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        hits = [_make_hit("a"), _make_hit("b"), _make_hit("c")]
        result = await filter_by_policy(hits, "agent-1", "admin", "search")
        assert len(result) == 3

    async def test_default_classification_is_internal(self, _patch_http):
        """Hits without classification should default to 'internal'."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        hit = SimpleNamespace(id="x", payload={})
        await filter_by_policy([hit], "agent-1", "analyst", "search")

        call_args = _patch_http.post.call_args
        input_data = call_args[1]["json"]["input"]
        assert input_data["classification"] == "internal"

    async def test_resource_prefix_is_used_in_policy_check(self, _patch_http):
        """filter_by_policy should pass resource_prefix/hit.id to check_opa_policy."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        hit = SimpleNamespace(id="doc-42", payload={"classification": "public"})
        await filter_by_policy([hit], "agent-1", "analyst", "collection_x")

        call_args = _patch_http.post.call_args
        input_data = call_args[1]["json"]["input"]
        assert input_data["resource"] == "collection_x/doc-42"
