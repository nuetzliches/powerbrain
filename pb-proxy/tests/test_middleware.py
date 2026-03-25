"""Tests for ProxyAuthMiddleware."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_scope(path="/v1/models", method="GET", headers=None):
    """Create a minimal ASGI HTTP scope."""
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append([k.lower().encode(), v.encode()])
    return {
        "type": "http",
        "path": path,
        "method": method,
        "headers": raw_headers,
        "state": {},
    }


async def _collect_response(middleware, scope):
    """Run middleware and collect response status + body."""
    response_started = {}
    response_body = b""

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        nonlocal response_body
        if message["type"] == "http.response.start":
            response_started["status"] = message["status"]
            response_started["headers"] = {
                k.decode(): v.decode()
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response_body = message.get("body", b"")

    await middleware(scope, receive, send)
    return response_started, response_body


@pytest.fixture
def mock_verifier():
    """Mock ProxyKeyVerifier that accepts pb_valid_key."""
    verifier = AsyncMock()

    async def _verify(token):
        if token == "pb_valid_key_123456789012345678901":
            return {"agent_id": "test-agent", "agent_role": "analyst"}
        return None

    verifier.verify = AsyncMock(side_effect=_verify)
    return verifier


@pytest.fixture
def mock_app():
    """Mock downstream ASGI app that records scope state."""
    calls = []

    async def app(scope, receive, send):
        calls.append(dict(scope.get("state", {})))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok": true}'})

    app.calls = calls
    return app


class TestMiddlewareAuthRequired:
    """Tests with auth_required=True."""

    def _make_middleware(self, app, verifier):
        from middleware import ProxyAuthMiddleware
        return ProxyAuthMiddleware(app, key_verifier=verifier, auth_required=True)

    @pytest.mark.asyncio
    async def test_rejects_no_auth_header(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/v1/models")
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401
        assert json.loads(body)["detail"] == "Authentication required"
        assert resp["headers"]["www-authenticate"] == "Bearer"
        assert len(mock_app.calls) == 0

    @pytest.mark.asyncio
    async def test_rejects_invalid_key(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(
            path="/v1/models",
            headers={"Authorization": "Bearer pb_bad_key_does_not_exist_1234567"},
        )
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401
        assert json.loads(body)["detail"] == "Invalid or expired API key"

    @pytest.mark.asyncio
    async def test_passes_valid_key(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(
            path="/v1/models",
            headers={"Authorization": "Bearer pb_valid_key_123456789012345678901"},
        )
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200
        assert len(mock_app.calls) == 1
        assert mock_app.calls[0]["agent_id"] == "test-agent"
        assert mock_app.calls[0]["agent_role"] == "analyst"
        assert mock_app.calls[0]["bearer_token"] == "pb_valid_key_123456789012345678901"

    @pytest.mark.asyncio
    async def test_skips_health_endpoint(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/health")
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200
        assert len(mock_app.calls) == 1
        assert mock_app.calls[0]["agent_id"] == "anonymous"
        mock_verifier.verify.assert_not_called()

    @pytest.mark.asyncio
    async def test_protects_metrics_json(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/metrics/json")
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401

    @pytest.mark.asyncio
    async def test_protects_v1_models(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/v1/models")
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401

    @pytest.mark.asyncio
    async def test_ignores_non_http_scope(self, mock_app, mock_verifier):
        """WebSocket and lifespan scopes pass through without auth."""
        from middleware import ProxyAuthMiddleware
        calls = []
        async def passthrough_app(scope, receive, send):
            calls.append(True)
        mw = ProxyAuthMiddleware(passthrough_app, key_verifier=mock_verifier, auth_required=True)
        scope = {"type": "lifespan"}
        await mw(scope, None, None)
        assert len(calls) == 1


class TestMiddlewareAuthDisabled:
    """Tests with auth_required=False."""

    def _make_middleware(self, app, verifier):
        from middleware import ProxyAuthMiddleware
        return ProxyAuthMiddleware(app, key_verifier=verifier, auth_required=False)

    @pytest.mark.asyncio
    async def test_allows_anonymous(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/v1/models")
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200
        assert mock_app.calls[0]["agent_id"] == "anonymous"
        assert mock_app.calls[0]["agent_role"] == "developer"
        mock_verifier.verify.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_metrics_anonymous(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/metrics/json")
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200