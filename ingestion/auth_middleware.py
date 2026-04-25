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

log = logging.getLogger("pb-ingestion.auth")


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
            return await self._send_401(send, "Authentication required")

        if not hmac.compare_digest(bearer_token, self._token):
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
