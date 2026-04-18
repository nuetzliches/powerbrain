"""Tests for B-41 Transparency Report (EU AI Act Art. 13).

Covers the module-level helpers and the get_system_info MCP tool. The
/transparency Starlette route is defined inside ``if __name__ == "__main__"``
and is exercised by the E2E smoke tests; here we verify that it is NOT in
AUTH_BYPASS_PATHS (auth-required), via a static source check.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import server
from server import (
    _TRANSPARENCY_CACHE,
    _build_transparency_payload,
    _compute_transparency_version,
    _dispatch,
    _get_transparency_report,
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

    # Reset module-level cache for every test
    _TRANSPARENCY_CACHE["payload"] = None
    _TRANSPARENCY_CACHE["version"] = None
    _TRANSPARENCY_CACHE["built_at"] = 0.0

    # Mock Qdrant client
    mock_qdrant = AsyncMock()
    collection_info = MagicMock()
    collection_info.status = "green"
    collection_info.points_count = 42
    collection_info.vectors_count = 42
    mock_qdrant.get_collection.return_value = collection_info
    monkeypatch.setattr(server, "qdrant", mock_qdrant)

    return mock_http, mock_pool, mock_qdrant


def _opa_config_response(cfg: dict | None = None):
    """HTTP 200 response matching OPA's /v1/data/pb/config shape."""
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = 200
    r.headers = {"content-type": "application/json"}
    r.json.return_value = {
        "result": cfg if cfg is not None else {
            "roles": ["viewer", "analyst", "developer", "admin"],
            "classifications": ["public", "internal", "confidential", "restricted"],
            "pii_entity_types": ["PERSON", "EMAIL_ADDRESS"],
            "audit": {"retention_days": 365, "advisory_lock_id": 847291},
            "summarization": {"denied_roles": ["viewer"]},
            "proxy": {"allowed_roles": ["analyst", "developer", "admin"]},
        }
    }
    return r


def _ingestion_health_response(body: dict | None = None):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = 200
    r.headers = {"content-type": "application/json"}
    r.json.return_value = body or {
        "pii_scanner_enabled": True,
        "pii_languages": ["en", "de"],
        "pii_entity_types": ["PERSON", "EMAIL_ADDRESS"],
    }
    return r


def _chain_row(valid=True, total=5):
    row = MagicMock()
    row.__getitem__ = lambda s, k: {"valid": valid, "total_checked": total}[k]
    return row


class TestComputeTransparencyVersion:
    def test_deterministic(self):
        models = {"embedding": {"name": "a"}, "llm": {"name": "b"}}
        v1 = _compute_transparency_version(models, "opa-hash", ["pb_general"])
        v2 = _compute_transparency_version(models, "opa-hash", ["pb_general"])
        assert v1 == v2
        assert len(v1) == 16

    def test_changes_with_models(self):
        v1 = _compute_transparency_version({"x": 1}, "a", ["c"])
        v2 = _compute_transparency_version({"x": 2}, "a", ["c"])
        assert v1 != v2

    def test_changes_with_opa_hash(self):
        v1 = _compute_transparency_version({}, "a", ["c"])
        v2 = _compute_transparency_version({}, "b", ["c"])
        assert v1 != v2

    def test_collection_order_ignored(self):
        v1 = _compute_transparency_version({}, "a", ["b", "c"])
        v2 = _compute_transparency_version({}, "a", ["c", "b"])
        assert v1 == v2


class TestBuildTransparencyPayload:
    async def test_happy_path(self, _patch_globals):
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "opa" in url or "8181" in url or "/v1/data/pb/config" in url:
                return _opa_config_response()
            if "ingestion" in url or "8081" in url:
                return _ingestion_health_response()
            raise AssertionError(f"unexpected URL: {url}")
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row(valid=True, total=10)

        payload = await _build_transparency_payload()

        assert payload["service"] == "mcp-server"
        # mcp-server always labels itself community; pb-proxy labels
        # enterprise. Demos detect the pair by hitting both endpoints.
        assert payload["edition"] == "community"
        assert "report_version" in payload and len(payload["report_version"]) == 16
        assert "system_purpose" in payload
        assert isinstance(payload["deployment_constraints"], list)
        assert payload["models"]["embedding"]["name"] == server.EMBEDDING_MODEL
        assert payload["models"]["llm"]["name"] == server.LLM_MODEL
        assert payload["opa"]["roles"] == ["viewer", "analyst", "developer", "admin"]
        assert payload["opa"]["audit_retention_days"] == 365
        assert len(payload["collections"]) == 3
        assert payload["pii_scanner"]["scanner_enabled"] is True
        assert payload["audit_integrity"]["valid"] is True
        assert payload["audit_integrity"]["total_checked"] == 10
        assert payload["risk_register"] == "docs/risk-management.md"

    async def test_opa_down_graceful(self, _patch_globals):
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "/v1/data/pb/config" in url:
                raise Exception("opa down")
            return _ingestion_health_response()
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row()

        payload = await _build_transparency_payload()
        assert payload["opa"]["roles"] == []
        assert "report_version" in payload

    async def test_broken_chain_surfaced(self, _patch_globals):
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "/v1/data/pb/config" in url:
                return _opa_config_response()
            return _ingestion_health_response()
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row(valid=False, total=3)

        payload = await _build_transparency_payload()
        assert payload["audit_integrity"]["valid"] is False

    async def test_no_secrets_exposed(self, _patch_globals):
        """Transparency report must not leak raw access_matrix or secrets."""
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "/v1/data/pb/config" in url:
                return _opa_config_response(cfg={
                    "roles": ["admin"],
                    "classifications": ["public"],
                    "pii_entity_types": [],
                    "audit": {"retention_days": 365},
                    "summarization": {},
                    "proxy": {"allowed_roles": []},
                    "access_matrix": {"secret_internal_mapping": ["nope"]},
                })
            return _ingestion_health_response()
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row()

        payload = await _build_transparency_payload()
        serialized = json.dumps(payload)
        assert "access_matrix" not in serialized
        assert "secret_internal_mapping" not in serialized


class TestTransparencyCache:
    async def test_cache_hit_second_call(self, _patch_globals):
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "/v1/data/pb/config" in url:
                return _opa_config_response()
            return _ingestion_health_response()
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row()

        p1 = await _get_transparency_report()
        calls_after_first = mock_http.get.call_count
        p2 = await _get_transparency_report()

        assert p1 is p2  # same cached object
        assert mock_http.get.call_count == calls_after_first  # no new http calls

    async def test_cache_expires(self, _patch_globals, monkeypatch):
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "/v1/data/pb/config" in url:
                return _opa_config_response()
            return _ingestion_health_response()
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row()

        monkeypatch.setattr(server, "TRANSPARENCY_CACHE_TTL", 0.001)
        await _get_transparency_report()
        first_calls = mock_http.get.call_count

        import time as _time
        _time.sleep(0.01)
        await _get_transparency_report()
        assert mock_http.get.call_count > first_calls


class TestGetSystemInfoTool:
    async def test_dispatch_returns_payload(self, _patch_globals):
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "/v1/data/pb/config" in url:
                return _opa_config_response()
            return _ingestion_health_response()
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row()

        result = await _dispatch("get_system_info", {}, "any-agent", "analyst")
        payload = json.loads(result[0].text)
        assert payload["service"] == "mcp-server"
        assert "report_version" in payload
        assert "models" in payload

    async def test_accessible_to_non_admin(self, _patch_globals):
        """Auth-required but not admin-only — every authenticated role may read."""
        mock_http, mock_pool, _ = _patch_globals

        async def _get(url, **kwargs):
            if "/v1/data/pb/config" in url:
                return _opa_config_response()
            return _ingestion_health_response()
        mock_http.get.side_effect = _get
        mock_pool.fetchrow.return_value = _chain_row()

        for role in ("viewer", "analyst", "developer", "admin"):
            result = await _dispatch("get_system_info", {}, f"{role}-1", role)
            payload = json.loads(result[0].text)
            assert "error" not in payload


class TestTransparencyRouteAuth:
    """Static source check: /transparency must NOT be in AUTH_BYPASS_PATHS."""

    def test_not_in_auth_bypass(self):
        source = Path(server.__file__).read_text()
        # Locate the AUTH_BYPASS_PATHS literal
        assert "AUTH_BYPASS_PATHS = {" in source
        # Find the set body
        start = source.index("AUTH_BYPASS_PATHS = {")
        end   = source.index("}", start)
        body  = source[start:end]
        assert "/transparency" not in body, (
            "/transparency must be auth-required — do not add it to "
            "AUTH_BYPASS_PATHS."
        )
