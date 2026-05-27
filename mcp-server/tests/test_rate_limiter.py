"""Tests for TokenBucket rate limiter."""

import asyncio
import logging

import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.authentication import UnauthenticatedUser

import server
from server import TokenBucket


class TestTokenBucket:
    @pytest.fixture
    async def bucket(self):
        """Bucket with capacity 3, refill 1 token/sec."""
        return TokenBucket(capacity=3.0, refill_rate=1.0)

    async def test_initial_tokens_available(self, bucket):
        allowed, retry_after = await bucket.consume()
        assert allowed is True
        assert retry_after == 0.0

    async def test_exhaust_capacity(self, bucket):
        for _ in range(3):
            allowed, _ = await bucket.consume()
            assert allowed is True

        allowed, retry_after = await bucket.consume()
        assert allowed is False
        assert retry_after > 0.0

    async def test_refill_after_wait(self, bucket):
        for _ in range(3):
            await bucket.consume()
        await asyncio.sleep(1.1)
        allowed, _ = await bucket.consume()
        assert allowed is True

    async def test_capacity_cap(self, bucket):
        """Tokens should not exceed capacity even after long wait."""
        await asyncio.sleep(0.5)
        results = [await bucket.consume() for _ in range(4)]
        allowed_count = sum(1 for allowed, _ in results if allowed)
        assert allowed_count == 3

    async def test_retry_after_value(self):
        """retry_after should reflect time until next token."""
        bucket = TokenBucket(capacity=1.0, refill_rate=1.0)
        await bucket.consume()
        allowed, retry_after = await bucket.consume()
        assert allowed is False
        assert 0.0 < retry_after <= 1.0


def _make_user(client_id="agent-1", scopes=("analyst",)):
    token = AccessToken(token="t", client_id=client_id, scopes=list(scopes), expires_at=None)
    return AuthenticatedUser(token)


class _DummyApp:
    """Downstream ASGI app that records how often it is invoked."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _drive(middleware, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await middleware(scope, receive, send)
    return sent


def _status(sent):
    for msg in sent:
        if msg["type"] == "http.response.start":
            return msg["status"]
    return None


@pytest.fixture
def rate_limit_env(monkeypatch):
    """Enable rate limiting with a tiny per-role capacity for fast tests."""
    monkeypatch.setattr(server, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(server, "RATE_LIMIT_ANALYST", 2)
    monkeypatch.setattr(
        server, "RATE_LIMITS_BY_ROLE", {"analyst": 2, "developer": 2, "admin": 2}
    )
    server._rate_limit_buckets.clear()
    yield
    server._rate_limit_buckets.clear()


class TestRateLimitMiddleware:
    """Regression tests for the bug where the middleware read the SDK user's
    `identity` property — which raises NotImplementedError — causing every
    authenticated request to fail open and silently disable rate limiting."""

    async def test_sdk_user_identity_raises(self):
        """The MCP SDK user exposes the agent id as `username`; `identity` is
        inherited from Starlette BaseUser and raises. This is the trap the
        middleware must avoid."""
        user = _make_user()
        assert user.username == "agent-1"
        with pytest.raises(NotImplementedError):
            _ = user.identity

    async def test_authenticated_user_enforces_limit(self, rate_limit_env):
        app = _DummyApp()
        middleware = server.RateLimitMiddleware(app)
        scope = {"type": "http", "path": "/mcp", "user": _make_user()}

        for _ in range(2):  # capacity is 2
            sent = await _drive(middleware, scope)
            assert _status(sent) == 200

        sent = await _drive(middleware, scope)
        assert _status(sent) == 429
        assert app.calls == 2  # third request never reached downstream

    async def test_no_fail_open_warning_for_authenticated_user(
        self, rate_limit_env, caplog
    ):
        app = _DummyApp()
        middleware = server.RateLimitMiddleware(app)
        scope = {"type": "http", "path": "/mcp", "user": _make_user()}

        with caplog.at_level(logging.WARNING, logger="pb-mcp"):
            await _drive(middleware, scope)

        assert "Rate limiter error" not in caplog.text

    async def test_per_agent_isolation(self, rate_limit_env):
        app = _DummyApp()
        middleware = server.RateLimitMiddleware(app)

        # Exhaust agent-1's bucket (capacity 2).
        scope1 = {"type": "http", "path": "/mcp", "user": _make_user("agent-1")}
        for _ in range(2):
            await _drive(middleware, scope1)
        assert _status(await _drive(middleware, scope1)) == 429

        # agent-2 has its own bucket and is unaffected.
        scope2 = {"type": "http", "path": "/mcp", "user": _make_user("agent-2")}
        assert _status(await _drive(middleware, scope2)) == 200

    async def test_unauthenticated_user_passes_through(self, rate_limit_env):
        app = _DummyApp()
        middleware = server.RateLimitMiddleware(app)
        scope = {"type": "http", "path": "/mcp", "user": UnauthenticatedUser()}

        sent = await _drive(middleware, scope)
        assert _status(sent) == 200
        assert app.calls == 1
        assert server._rate_limit_buckets == {}  # no bucket created

    async def test_health_path_skipped(self, rate_limit_env):
        app = _DummyApp()
        middleware = server.RateLimitMiddleware(app)
        scope = {"type": "http", "path": "/health", "user": _make_user()}

        sent = await _drive(middleware, scope)
        assert _status(sent) == 200
        assert server._rate_limit_buckets == {}
