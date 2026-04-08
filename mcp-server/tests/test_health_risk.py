"""Tests for B-44 enhanced /health risk indicators.

The health_check request handler is defined inside ``if __name__ == "__main__"``
so we test the module-level helpers (``_build_risk_health_payload`` and the
individual ``_check_*`` functions) directly.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import server
from server import (
    _build_risk_health_payload,
    _check_audit_chain,
    _check_feedback_score,
    _check_opa_reachable,
    _check_pii_scanner,
    _check_reranker_available,
    _worst_severity,
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
    return mock_http, mock_pool


def _ok_response(json_body=None):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = 200
    r.headers = {"content-type": "application/json"}
    r.json.return_value = json_body or {}
    return r


class TestWorstSeverity:
    def test_ok_vs_warning(self):
        assert _worst_severity("ok", "warning") == "warning"

    def test_critical_wins(self):
        assert _worst_severity("warning", "critical") == "critical"
        assert _worst_severity("critical", "ok") == "critical"

    def test_ok_ok(self):
        assert _worst_severity("ok", "ok") == "ok"

    def test_unknown_treated_as_ok(self):
        assert _worst_severity("nonsense", "warning") == "warning"


class TestOpaReachable:
    async def test_reachable(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.get.return_value = _ok_response()
        result = await _check_opa_reachable()
        assert result["value"] is True
        assert result["severity"] == "ok"
        assert result["risk"] == "R-05"

    async def test_unreachable_is_critical(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.get.side_effect = Exception("connection refused")
        result = await _check_opa_reachable()
        assert result["value"] is False
        assert result["severity"] == "critical"
        assert "connection refused" in result["detail"]


class TestPiiScanner:
    async def test_enabled(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.get.return_value = _ok_response({"pii_scanner_enabled": True})
        result = await _check_pii_scanner()
        assert result["value"] == "enabled"
        assert result["severity"] == "ok"

    async def test_disabled_is_high(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.get.return_value = _ok_response({"pii_scanner_enabled": False})
        result = await _check_pii_scanner()
        assert result["value"] == "disabled"
        assert result["severity"] == "high"

    async def test_unreachable_is_high(self, _patch_globals):
        mock_http, _ = _patch_globals
        mock_http.get.side_effect = Exception("timeout")
        result = await _check_pii_scanner()
        assert result["value"] == "unreachable"
        assert result["severity"] == "high"


class TestRerankerAvailable:
    async def test_disabled_is_info(self, _patch_globals, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", False)
        result = await _check_reranker_available()
        assert result["value"] == "disabled"
        assert result["severity"] == "info"

    async def test_available(self, _patch_globals, monkeypatch):
        mock_http, _ = _patch_globals
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        mock_http.get.return_value = _ok_response()
        result = await _check_reranker_available()
        assert result["value"] is True
        assert result["severity"] == "ok"

    async def test_unavailable_is_medium(self, _patch_globals, monkeypatch):
        mock_http, _ = _patch_globals
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        mock_http.get.side_effect = Exception("down")
        result = await _check_reranker_available()
        assert result["value"] is False
        assert result["severity"] == "medium"


class TestAuditChain:
    async def test_valid_chain(self, _patch_globals):
        _, mock_pool = _patch_globals
        row = MagicMock()
        row.__getitem__ = lambda s, k: {"valid": True, "first_invalid_id": None,
                                         "total_checked": 100}[k]
        mock_pool.fetchrow.return_value = row
        result = await _check_audit_chain()
        assert result["value"] == "valid"
        assert result["severity"] == "ok"
        assert result["total_checked"] == 100

    async def test_invalid_chain_is_critical(self, _patch_globals):
        _, mock_pool = _patch_globals
        row = MagicMock()
        row.__getitem__ = lambda s, k: {"valid": False, "first_invalid_id": 42,
                                         "total_checked": 41}[k]
        mock_pool.fetchrow.return_value = row
        result = await _check_audit_chain()
        assert result["value"] == "invalid"
        assert result["severity"] == "critical"
        assert result["first_invalid_id"] == 42

    async def test_db_error_is_warning(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.side_effect = Exception("db down")
        result = await _check_audit_chain()
        assert result["severity"] == "warning"


class TestFeedbackScore:
    async def test_high_score_ok(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchval.return_value = 4.2
        result = await _check_feedback_score()
        assert result["value"] == 4.2
        assert result["severity"] == "ok"

    async def test_low_score_warning(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchval.return_value = 2.1
        result = await _check_feedback_score()
        assert result["value"] == 2.1
        assert result["severity"] == "warning"

    async def test_no_data_ok(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchval.return_value = None
        result = await _check_feedback_score()
        assert result["value"] is None
        assert result["severity"] == "ok"


class TestBuildRiskHealthPayload:
    async def test_all_green(self, _patch_globals, monkeypatch):
        mock_http, mock_pool = _patch_globals
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        mock_http.get.return_value = _ok_response({"pii_scanner_enabled": True})
        row = MagicMock()
        row.__getitem__ = lambda s, k: {"valid": True, "first_invalid_id": None,
                                         "total_checked": 5}[k]
        mock_pool.fetchrow.return_value = row
        mock_pool.fetchval.return_value = 4.5

        payload = await _build_risk_health_payload()
        assert payload["service"] == "mcp-server"
        assert payload["status"] == "info"  # circuit_breaker indicator is info
        names = {i["name"] for i in payload["indicators"]}
        assert names == {
            "opa_reachable", "pii_scanner_status", "reranker_available",
            "audit_chain_integrity", "feedback_score", "circuit_breaker_active",
        }

    async def test_critical_on_opa_down(self, _patch_globals, monkeypatch):
        mock_http, mock_pool = _patch_globals
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        # OPA call fails, other calls succeed
        call_count = {"n": 0}
        def _get(url, **kwargs):
            call_count["n"] += 1
            if "opa" in url.lower() or "8181" in url:
                raise Exception("opa down")
            return _ok_response({"pii_scanner_enabled": True})
        mock_http.get.side_effect = _get

        row = MagicMock()
        row.__getitem__ = lambda s, k: {"valid": True, "first_invalid_id": None,
                                         "total_checked": 5}[k]
        mock_pool.fetchrow.return_value = row
        mock_pool.fetchval.return_value = 4.5

        payload = await _build_risk_health_payload()
        assert payload["status"] == "critical"
        opa_ind = next(i for i in payload["indicators"] if i["name"] == "opa_reachable")
        assert opa_ind["severity"] == "critical"

    async def test_critical_on_broken_chain(self, _patch_globals, monkeypatch):
        mock_http, mock_pool = _patch_globals
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        mock_http.get.return_value = _ok_response({"pii_scanner_enabled": True})

        row = MagicMock()
        row.__getitem__ = lambda s, k: {"valid": False, "first_invalid_id": 99,
                                         "total_checked": 98}[k]
        mock_pool.fetchrow.return_value = row
        mock_pool.fetchval.return_value = 4.5

        payload = await _build_risk_health_payload()
        assert payload["status"] == "critical"
        chain_ind = next(i for i in payload["indicators"]
                         if i["name"] == "audit_chain_integrity")
        assert chain_ind["value"] == "invalid"
        assert chain_ind["first_invalid_id"] == 99

    async def test_risk_register_reference_included(self, _patch_globals, monkeypatch):
        mock_http, mock_pool = _patch_globals
        monkeypatch.setattr(server, "RERANKER_ENABLED", False)
        mock_http.get.return_value = _ok_response({"pii_scanner_enabled": True})

        row = MagicMock()
        row.__getitem__ = lambda s, k: {"valid": True, "first_invalid_id": None,
                                         "total_checked": 0}[k]
        mock_pool.fetchrow.return_value = row
        mock_pool.fetchval.return_value = None

        payload = await _build_risk_health_payload()
        assert payload["risk_register"] == "docs/risk-management.md"
