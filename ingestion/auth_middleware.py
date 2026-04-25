"""
Ingestion service authentication middleware.

Pure ASGI middleware that requires an ``Authorization: Bearer <token>``
header on every internal endpoint. Used as defense-in-depth on top of
the Docker-network isolation: if pb-net is ever mis-scoped or a
container is exposed by accident, callers without the service token
get a 401 instead of free access to ``/ingest`` or ``/scan``.

Mirrors the pattern of ``pb-proxy/middleware.py`` (ProxyAuthMiddleware).

Back-compat: when ``INGESTION_AUTH_TOKEN`` is empty, the middleware
logs a loud warning at startup and allows everything. This keeps
existing deployments running on upgrade until they roll a token.
"""

from __future__ import annotations

import hmac
import json
import logging

from prometheus_client import Counter, REGISTRY

log = logging.getLogger("pb-ingestion.auth")


def _get_or_create_counter() -> Counter:
    """Idempotent Counter registration so re-imports during tests don't
    raise ``Duplicated timeseries`` against the global Prometheus
    registry (mirrors the pattern in ingestion_api.py)."""
    name = "pb_ingestion_auth_failures_total"
    try:
        return Counter(
            name,
            "Ingestion service-token auth failures",
            ["reason"],
        )
    except ValueError as exc:
        if "Duplicated timeseries" not in str(exc):
            raise
        for collector in list(REGISTRY._collector_to_names.keys()):
            if getattr(collector, "_name", None) == name:
                return collector  # type: ignore[return-value]
        raise


pb_ingestion_auth_failures = _get_or_create_counter()


class IngestionAuthMiddleware:
    """ASGI middleware enforcing a service-token bearer on protected paths.

    Args:
        app: downstream ASGI application.
        expected_token: shared service token. When empty, the middleware
            falls back to allow-all mode and logs a warning once.
    """

    EXEMPT_PATHS = {"/health", "/metrics", "/metrics/json"}

    def __init__(self, app, expected_token: str) -> None:
        self.app = app
        self._token = expected_token or ""
        if not self._token:
            log.warning(
                "INGESTION_AUTH_TOKEN is not set — internal endpoints are "
                "unauthenticated. Set the token via Docker Secret "
                "(/run/secrets/ingestion_auth_token) for defense-in-depth. "
                "See docs/BACKLOG.md B-50."
            )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in self.EXEMPT_PATHS or path.startswith("/metrics/"):
            return await self.app(scope, receive, send)

        if not self._token:
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()
        bearer_token = ""
        if auth_value.lower().startswith("bearer "):
            bearer_token = auth_value[7:].strip()

        if not bearer_token:
            pb_ingestion_auth_failures.labels(reason="missing").inc()
            return await self._send_401(send, "Authentication required")

        if not hmac.compare_digest(bearer_token, self._token):
            pb_ingestion_auth_failures.labels(reason="invalid").inc()
            return await self._send_401(send, "Invalid service token")

        return await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"www-authenticate", b"Bearer"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body})
