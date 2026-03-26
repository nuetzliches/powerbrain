"""
Proxy authentication middleware.
Pure ASGI middleware that validates pb_ API keys on all routes
except whitelisted paths (e.g. /health).
"""

import json
import logging

import config as _config

log = logging.getLogger("pb-proxy.middleware")


class ProxyAuthMiddleware:
    """Global ASGI middleware for pb_ API key authentication.

    Applied to all HTTP routes except whitelisted paths.
    On success: populates scope["state"] with agent_id, agent_role, bearer_token.
    On failure: sends 401 JSON response with WWW-Authenticate header.
    Non-HTTP scopes (lifespan, websocket) pass through unchanged.
    """

    WHITELIST = {"/health"}

    def __init__(self, app, key_verifier) -> None:
        self.app = app
        self.key_verifier = key_verifier

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # Whitelisted paths and auth-disabled mode: set anonymous defaults
        if path in self.WHITELIST or not _config.AUTH_REQUIRED:
            scope.setdefault("state", {})
            scope["state"]["agent_id"] = "anonymous"
            scope["state"]["agent_role"] = "developer"
            scope["state"]["bearer_token"] = None
            return await self.app(scope, receive, send)

        # Extract Bearer token
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()
        bearer_token = None
        if auth_value.lower().startswith("bearer "):
            bearer_token = auth_value[7:].strip()

        if not bearer_token:
            return await self._send_401(send, "Authentication required")

        verified = await self.key_verifier.verify(bearer_token)
        if verified is None:
            return await self._send_401(send, "Invalid or expired API key")

        # Populate scope state for downstream FastAPI handlers
        scope.setdefault("state", {})
        scope["state"]["agent_id"] = verified["agent_id"]
        scope["state"]["agent_role"] = verified["agent_role"]
        scope["state"]["bearer_token"] = bearer_token

        log.info("Authenticated: agent_id=%s, agent_role=%s",
                 verified["agent_id"], verified["agent_role"])

        return await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send, detail: str) -> None:
        """Send a 401 JSON response at the ASGI level."""
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