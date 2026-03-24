"""Tests for OPA privacy policy checking in ingestion."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import ingestion_api
from ingestion_api import check_opa_privacy


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(ingestion_api, "http_client", mock_client)
    return mock_client


class TestCheckOpaPrivacy:
    async def test_returns_policy_result(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "result": {
                "pii_action": "pseudonymize",
                "dual_storage_enabled": True,
                "retention_days": 180,
            }
        }
        _patch_http.post.return_value = response

        result = await check_opa_privacy("internal", True, "consent")

        assert result["pii_action"] == "pseudonymize"
        assert result["dual_storage_enabled"] is True
        assert result["retention_days"] == 180

    async def test_defaults_to_block_on_error(self, _patch_http):
        _patch_http.post.side_effect = Exception("OPA unreachable")

        result = await check_opa_privacy("internal", True)

        assert result["pii_action"] == "block"
        assert result["dual_storage_enabled"] is False

    async def test_calls_correct_endpoint(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": {"pii_action": "redact"}}
        _patch_http.post.return_value = response

        await check_opa_privacy("confidential", False, "legal_obligation")

        call_args = _patch_http.post.call_args
        assert "/v1/data/pb/privacy" in call_args[0][0]
        input_data = call_args[1]["json"]["input"]
        assert input_data["classification"] == "confidential"
        assert input_data["contains_pii"] is False

    async def test_legal_basis_defaults_to_empty(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": {"pii_action": "mask"}}
        _patch_http.post.return_value = response

        await check_opa_privacy("public", True)

        call_args = _patch_http.post.call_args
        input_data = call_args[1]["json"]["input"]
        assert input_data["legal_basis"] == ""

    async def test_missing_fields_use_defaults(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": {}}
        _patch_http.post.return_value = response

        result = await check_opa_privacy("internal", True)

        assert result["pii_action"] == "block"
        assert result["dual_storage_enabled"] is False
        assert result["retention_days"] == 365

    async def test_retention_days_from_response(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "result": {
                "pii_action": "pseudonymize",
                "dual_storage_enabled": True,
                "retention_days": 90,
            }
        }
        _patch_http.post.return_value = response

        result = await check_opa_privacy("confidential", True, "consent")

        assert result["retention_days"] == 90
