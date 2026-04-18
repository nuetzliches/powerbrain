"""Tests for B-12: manage_policies MCP tool."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import server


@pytest.fixture(autouse=True)
def _ensure_schema_loaded(monkeypatch):
    """Ensure _POLICY_SECTION_PROPS is populated from real schema."""
    if not server._POLICY_SECTION_PROPS:
        # Load schema from repo path for tests running outside Docker
        import os
        schema_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "opa-policies", "policy_data_schema.json",
        )
        with open(schema_path) as f:
            schema = json.load(f)
        props = (
            schema.get("properties", {}).get("pb", {})
            .get("properties", {}).get("config", {})
            .get("properties", {})
        )
        monkeypatch.setattr(server, "_POLICY_SECTION_PROPS", props)
        monkeypatch.setattr(server, "_POLICY_SCHEMA", schema)

    # Stub out log_access (needs PG pool + ingestion) and get_pg_pool
    async def _noop_log(*args, **kwargs):
        return None
    monkeypatch.setattr(server, "log_access", _noop_log)

    mock_pool = AsyncMock()
    async def _fake_get_pool():
        return mock_pool
    monkeypatch.setattr(server, "get_pg_pool", _fake_get_pool)


def _make_opa_response(data):
    """Create a mock HTTP response with OPA-style result."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"result": data}
    return resp


def _make_put_response():
    """Create a mock HTTP response for a successful PUT."""
    resp = MagicMock()
    resp.status_code = 204
    resp.raise_for_status = MagicMock()
    return resp


class TestManagePoliciesList:

    @pytest.mark.asyncio
    async def test_list_returns_all_sections(self, mock_http_client, monkeypatch):
        monkeypatch.setattr(server, "http", mock_http_client)
        # Mock get_access_token
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        result = await server.call_tool("manage_policies", {"action": "list"})
        data = json.loads(result[0].text)
        assert "sections" in data
        # All 17 required sections should be listed
        assert "roles" in data["sections"]
        assert "access_matrix" in data["sections"]
        assert "audit" in data["sections"]
        assert "rules" in data["sections"]
        assert len(data["sections"]) >= 17

    @pytest.mark.asyncio
    async def test_non_admin_denied(self, mock_http_client, monkeypatch):
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "analyst-1"
        token.scopes = ["analyst"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        result = await server.call_tool("manage_policies", {"action": "list"})
        data = json.loads(result[0].text)
        assert "error" in data
        assert "admin" in data["error"]


class TestManagePoliciesRead:

    @pytest.mark.asyncio
    async def test_read_returns_section_data(self, mock_http_client, monkeypatch):
        mock_http_client.get.return_value = _make_opa_response(
            ["viewer", "analyst", "developer", "admin"]
        )
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        result = await server.call_tool("manage_policies", {
            "action": "read", "section": "roles"
        })
        data = json.loads(result[0].text)
        assert data["section"] == "roles"
        assert data["data"] == ["viewer", "analyst", "developer", "admin"]

    @pytest.mark.asyncio
    async def test_read_invalid_section(self, mock_http_client, monkeypatch):
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        result = await server.call_tool("manage_policies", {
            "action": "read", "section": "nonexistent"
        })
        data = json.loads(result[0].text)
        assert "error" in data
        assert "nonexistent" in data["error"]
        assert "valid_sections" in data

    @pytest.mark.asyncio
    async def test_read_opa_failure(self, mock_http_client, monkeypatch):
        mock_http_client.get.side_effect = Exception("OPA unreachable")
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        result = await server.call_tool("manage_policies", {
            "action": "read", "section": "roles"
        })
        data = json.loads(result[0].text)
        assert "error" in data
        assert "Failed to read" in data["error"]


class TestManagePoliciesUpdate:

    @pytest.mark.asyncio
    async def test_update_valid_data(self, mock_http_client, monkeypatch):
        # GET for old value, PUT for write
        mock_http_client.get.return_value = _make_opa_response(
            ["viewer", "analyst", "developer", "admin"]
        )
        mock_http_client.put.return_value = _make_put_response()
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        new_roles = ["viewer", "analyst", "developer", "admin", "auditor"]
        result = await server.call_tool("manage_policies", {
            "action": "update", "section": "roles", "data": new_roles
        })
        data = json.loads(result[0].text)
        assert data["status"] == "updated"
        assert data["section"] == "roles"
        # Verify PUT was called with correct URL and data
        mock_http_client.put.assert_called_once()
        call_args = mock_http_client.put.call_args
        assert "roles" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_update_invalid_schema(self, mock_http_client, monkeypatch):
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        # roles must be an array, not a string
        result = await server.call_tool("manage_policies", {
            "action": "update", "section": "roles", "data": "not_an_array"
        })
        data = json.loads(result[0].text)
        assert "error" in data
        assert "Schema validation failed" in data["error"]
        # OPA should NOT have been called
        mock_http_client.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_empty_array_rejected(self, mock_http_client, monkeypatch):
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        # roles has minItems: 1, so empty array should fail
        result = await server.call_tool("manage_policies", {
            "action": "update", "section": "roles", "data": []
        })
        data = json.loads(result[0].text)
        assert "error" in data
        assert "Schema validation failed" in data["error"]

    @pytest.mark.asyncio
    async def test_update_missing_data(self, mock_http_client, monkeypatch):
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        result = await server.call_tool("manage_policies", {
            "action": "update", "section": "roles"
        })
        data = json.loads(result[0].text)
        assert "error" in data
        assert "Missing" in data["error"]

    @pytest.mark.asyncio
    async def test_update_invalidates_cache(self, mock_http_client, monkeypatch):
        mock_http_client.get.return_value = _make_opa_response(["admin"])
        mock_http_client.put.return_value = _make_put_response()
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        # Seed the cache
        server._opa_cache["test_key"] = True
        server._fields_to_redact_cache["test_key"] = {"email"}

        await server.call_tool("manage_policies", {
            "action": "update", "section": "roles",
            "data": ["viewer", "analyst", "admin"]
        })

        # Caches should be cleared
        assert len(server._opa_cache) == 0
        assert len(server._fields_to_redact_cache) == 0

    @pytest.mark.asyncio
    async def test_update_opa_write_failure(self, mock_http_client, monkeypatch):
        mock_http_client.get.return_value = _make_opa_response(["admin"])
        mock_http_client.put.side_effect = Exception("OPA write failed")
        monkeypatch.setattr(server, "http", mock_http_client)
        token = MagicMock()
        token.client_id = "admin-1"
        token.scopes = ["admin"]
        monkeypatch.setattr(server, "get_access_token", lambda: token)

        result = await server.call_tool("manage_policies", {
            "action": "update", "section": "roles",
            "data": ["viewer", "admin"]
        })
        data = json.loads(result[0].text)
        assert "error" in data
        assert "Failed to write" in data["error"]
