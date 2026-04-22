"""Regression tests for issue #59 part 2.

When an OPA policy package is not loaded, OPA returns a body without a
``result`` field. Before the fix, the ingestion helpers silently
collapsed this to ``allowed=False, min_score=0.0``, producing the
misleading log ``"quality_score 0.629 < required 0.000"`` that cost
operators hours of debugging. These tests lock in the new behaviour:

* ``check_opa_quality_gate`` returns the ``-1.0`` sentinel for
  ``min_score`` and a reason starting with ``"opa_policy_missing"``.
* ``check_opa_privacy`` defaults to ``block`` with an explicit reason.
* ``check_opa_pii_verifier`` falls back to the env defaults but logs
  the missing policy path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import ingestion_api


@pytest.fixture
def http_missing_policy(monkeypatch):
    """Return a mock client whose OPA response has no 'result' field."""
    mock = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"decision_id": "abc-no-policy"}
    mock.post.return_value = response
    monkeypatch.setattr(ingestion_api, "http_client", mock)
    return mock


class TestQualityGateMissingPolicy:

    async def test_returns_allowed_false(self, http_missing_policy):
        gate = await ingestion_api.check_opa_quality_gate("code", 0.75)
        assert gate["allowed"] is False

    async def test_min_score_is_sentinel(self, http_missing_policy):
        """Sentinel -1.0 makes it obvious the comparison is nonsense.

        A real threshold is always >= 0. Seeing ``min=-1.000`` in logs or
        the ``ingestion_rejections`` table immediately flags a
        configuration problem.
        """
        gate = await ingestion_api.check_opa_quality_gate("code", 0.75)
        assert gate["min_score"] == -1.0

    async def test_reason_names_missing_package(self, http_missing_policy):
        gate = await ingestion_api.check_opa_quality_gate("code", 0.75)
        assert gate["reason"].startswith("opa_policy_missing:")
        assert "pb/ingestion/quality_gate" in gate["reason"]


class TestPrivacyMissingPolicy:

    async def test_defaults_to_block(self, http_missing_policy):
        privacy = await ingestion_api.check_opa_privacy("internal", True, "consent")
        assert privacy["pii_action"] == "block"
        assert privacy["dual_storage_enabled"] is False

    async def test_reason_surfaces_missing_package(self, http_missing_policy):
        privacy = await ingestion_api.check_opa_privacy("internal", True)
        assert "reason" in privacy
        assert "opa_policy_missing" in privacy["reason"]
        assert "pb/privacy" in privacy["reason"]


class TestPIIVerifierMissingPolicy:
    """The verifier is optional — missing policy should fall back to env
    defaults rather than raise or block."""

    async def test_falls_back_to_env_defaults(self, http_missing_policy):
        got = await ingestion_api.check_opa_pii_verifier()
        assert "enabled" in got
        assert "backend" in got
        assert "min_confidence_keep" in got

    async def test_no_exception_propagates(self, http_missing_policy):
        # Missing policy must not surface as an exception from this
        # helper — the caller relies on always getting a dict.
        result = await ingestion_api.check_opa_pii_verifier()
        assert isinstance(result, dict)


class TestQualityGateDenyIsDistinctFromMissing:
    """A real deny decision must NOT be confused with a missing policy.

    If the policy returns {"allowed": false, "min_score": 0.5}, we
    should pass those values through unchanged — the existing
    ``test_denied_propagates`` in test_quality.py already covers the
    happy path; this test adds the contrast to the new sentinel.
    """

    async def test_explicit_deny_keeps_positive_min_score(self, monkeypatch):
        mock = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "result": {"allowed": False, "min_score": 0.5, "reason": "too_low"}
        }
        mock.post.return_value = response
        monkeypatch.setattr(ingestion_api, "http_client", mock)

        gate = await ingestion_api.check_opa_quality_gate("default", 0.3)

        assert gate["allowed"] is False
        assert gate["min_score"] == 0.5  # real threshold, not -1.0 sentinel
        assert gate["reason"] == "too_low"
