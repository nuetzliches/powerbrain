"""Boot-time check for INGESTION_AUTH_TOKEN configuration (issue #126).

Mirrors the OPA boot-check pattern (`shared/opa_client.verify_required_policies`).
A misconfigured `INGESTION_AUTH_TOKEN` would silently disable the ingestion
service's defense-in-depth auth layer (the middleware logs a warning and
allows everything). For a feature explicitly described as "defense-in-depth",
silent failure defeats the purpose.

This module surfaces the misconfiguration as a hard boot-time error when
`AUTH_REQUIRED=true` (the default) and the token is empty, so a typo in the
secret name or a forgotten rollout becomes immediately visible instead of
shipping a degraded-mode service that healthchecks happily report as green.

Operators have three escape hatches:
  - `AUTH_REQUIRED=false` — opts out of the auth layer entirely (test/dev).
  - `SKIP_INGESTION_AUTH_STARTUP_CHECK=true` — bypasses just this check
    (mirrors `SKIP_OPA_STARTUP_CHECK`; intended for unit tests).
  - Set the token. The fix.

The Prometheus gauge `pb_ingestion_auth_enabled{service=...}` exposes the
runtime state so operators can alert on degraded mode regardless of the
boot path.
"""

from __future__ import annotations

import logging

from prometheus_client import Gauge, REGISTRY


log = logging.getLogger("pb.ingestion_auth")


def _get_or_create_gauge() -> Gauge:
    """Idempotent Gauge registration so re-imports during tests don't
    raise ``Duplicated timeseries`` against the global registry."""
    name = "pb_ingestion_auth_enabled"
    try:
        return Gauge(
            name,
            "1 if INGESTION_AUTH_TOKEN is configured, 0 otherwise. "
            "Reports the boot-time decision per service.",
            ["service"],
        )
    except ValueError as exc:
        if "Duplicated timeseries" not in str(exc):
            raise
        for collector in list(REGISTRY._collector_to_names.keys()):
            if getattr(collector, "_name", None) == name:
                return collector  # type: ignore[return-value]
        raise


pb_ingestion_auth_enabled = _get_or_create_gauge()


class IngestionAuthMisconfiguredError(RuntimeError):
    """Raised when AUTH_REQUIRED=true but INGESTION_AUTH_TOKEN is empty.

    Surfaced at module import time so the service refuses to start
    instead of running in a silently-degraded mode (#126).
    """


def verify_ingestion_auth_configured(
    token: str,
    *,
    auth_required: bool,
    skip_check: bool,
    service_name: str,
) -> None:
    """Boot-time guard for INGESTION_AUTH_TOKEN. See module docstring.

    Args:
        token: The INGESTION_AUTH_TOKEN value (possibly empty).
        auth_required: The service's AUTH_REQUIRED flag.
        skip_check: Operator opt-out (`SKIP_INGESTION_AUTH_STARTUP_CHECK`),
            mirroring `SKIP_OPA_STARTUP_CHECK`.
        service_name: Service identifier for logging + metrics labels
            (e.g. "ingestion", "mcp-server").

    Raises:
        IngestionAuthMisconfiguredError: when ``auth_required`` is True,
            ``token`` is empty, and ``skip_check`` is False.
    """
    label = pb_ingestion_auth_enabled.labels(service=service_name)

    if not auth_required:
        log.warning(
            "[%s] AUTH_REQUIRED=false — INGESTION_AUTH_TOKEN check skipped. "
            "Run with AUTH_REQUIRED=true in production deployments.",
            service_name,
        )
        label.set(0)
        return

    if token:
        label.set(1)
        return

    if skip_check:
        log.warning(
            "[%s] SKIP_INGESTION_AUTH_STARTUP_CHECK=true — booting without "
            "INGESTION_AUTH_TOKEN despite AUTH_REQUIRED=true. Intended for "
            "unit tests only; do not set this in production.",
            service_name,
        )
        label.set(0)
        return

    label.set(0)
    raise IngestionAuthMisconfiguredError(
        f"[{service_name}] AUTH_REQUIRED=true but INGESTION_AUTH_TOKEN is "
        f"empty. The ingestion service auth layer would be silently "
        f"disabled. Provide the token (Docker Secret "
        f"/run/secrets/ingestion_auth_token, or env INGESTION_AUTH_TOKEN), "
        f"or set AUTH_REQUIRED=false to skip auth in test/dev. To bypass "
        f"this check (e.g. unit tests), set "
        f"SKIP_INGESTION_AUTH_STARTUP_CHECK=true."
    )
