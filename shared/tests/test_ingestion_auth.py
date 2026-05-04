"""Tests for the INGESTION_AUTH_TOKEN boot-time check (#126)."""

from __future__ import annotations

import pytest

from shared.ingestion_auth import (
    IngestionAuthMisconfiguredError,
    pb_ingestion_auth_enabled,
    verify_ingestion_auth_configured,
)


def _gauge_value(service: str) -> float:
    return pb_ingestion_auth_enabled.labels(service=service)._value.get()


class TestVerifyIngestionAuthConfigured:
    def test_token_set_passes_and_marks_enabled(self):
        verify_ingestion_auth_configured(
            "secret-token",
            auth_required=True,
            skip_check=False,
            service_name="test-token-set",
        )
        assert _gauge_value("test-token-set") == 1

    def test_empty_token_with_auth_required_raises(self):
        with pytest.raises(IngestionAuthMisconfiguredError) as exc:
            verify_ingestion_auth_configured(
                "",
                auth_required=True,
                skip_check=False,
                service_name="test-empty-token",
            )
        # Helpful diagnostic content in the error message
        assert "INGESTION_AUTH_TOKEN" in str(exc.value)
        assert "AUTH_REQUIRED" in str(exc.value)
        assert "SKIP_INGESTION_AUTH_STARTUP_CHECK" in str(exc.value)
        # Gauge marks it as disabled
        assert _gauge_value("test-empty-token") == 0

    def test_auth_not_required_passes_with_empty_token(self):
        verify_ingestion_auth_configured(
            "",
            auth_required=False,
            skip_check=False,
            service_name="test-auth-not-required",
        )
        # Gauge marks the disabled state so dashboards can alert on it
        assert _gauge_value("test-auth-not-required") == 0

    def test_skip_check_bypasses_with_empty_token(self):
        # Should not raise even though auth_required=True + token is empty
        verify_ingestion_auth_configured(
            "",
            auth_required=True,
            skip_check=True,
            service_name="test-skip-check",
        )
        assert _gauge_value("test-skip-check") == 0

    def test_skip_check_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="pb.ingestion_auth"):
            verify_ingestion_auth_configured(
                "",
                auth_required=True,
                skip_check=True,
                service_name="test-skip-warns",
            )
        msgs = " ".join(r.message for r in caplog.records)
        assert "SKIP_INGESTION_AUTH_STARTUP_CHECK" in msgs

    def test_auth_not_required_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="pb.ingestion_auth"):
            verify_ingestion_auth_configured(
                "any-token",
                auth_required=False,
                skip_check=False,
                service_name="test-not-required-warns",
            )
        msgs = " ".join(r.message for r in caplog.records)
        assert "AUTH_REQUIRED=false" in msgs

    def test_token_set_does_not_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="pb.ingestion_auth"):
            verify_ingestion_auth_configured(
                "real-token",
                auth_required=True,
                skip_check=False,
                service_name="test-no-warn",
            )
        # Happy path emits no log records
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_each_service_gets_its_own_gauge_label(self):
        verify_ingestion_auth_configured(
            "tok",
            auth_required=True,
            skip_check=False,
            service_name="svc-a",
        )
        verify_ingestion_auth_configured(
            "",
            auth_required=False,
            skip_check=False,
            service_name="svc-b",
        )
        assert _gauge_value("svc-a") == 1
        assert _gauge_value("svc-b") == 0

    def test_misconfigured_error_is_runtime_error(self):
        # Subclass relationship lets callers catch RuntimeError generically
        # if they prefer a broader except clause.
        assert issubclass(IngestionAuthMisconfiguredError, RuntimeError)
