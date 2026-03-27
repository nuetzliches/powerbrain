"""
E2E Smoke Tests for pb-proxy (AI Provider Proxy).

Tests require the full Docker Compose stack INCLUDING the proxy profile:
    docker compose --profile proxy up -d

Gated behind RUN_INTEGRATION_TESTS=1 (same as test_smoke.py).
"""

import os
import json

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION_TESTS") != "1",
        reason="Set RUN_INTEGRATION_TESTS=1 to run E2E tests",
    ),
]

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8090")


# ── Helpers ──────────────────────────────────────────────────

def _proxy_request(
    method: str,
    path: str,
    api_key: str | None = None,
    json_body: dict | None = None,
    timeout: float = 10,
) -> httpx.Response:
    """Send a request to the proxy."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.request(
        method, f"{PROXY_URL}{path}",
        headers=headers,
        json=json_body,
        timeout=timeout,
    )


def _is_proxy_running() -> bool:
    """Check if the proxy is reachable."""
    try:
        resp = httpx.get(f"{PROXY_URL}/health", timeout=3)
        return resp.status_code == 200
    except httpx.ConnectError:
        return False


# ── Health & Discovery ───────────────────────────────────────

@pytest.mark.skipif(
    not _is_proxy_running() if os.getenv("RUN_INTEGRATION_TESTS") == "1" else True,
    reason="Proxy service not running (start with: docker compose --profile proxy up -d)",
)
class TestProxyHealth:
    """Basic proxy availability tests."""

    def test_health_endpoint(self):
        resp = _proxy_request("GET", "/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("healthy", "degraded")
        assert "tools_loaded" in data

    def test_models_endpoint(self, api_key):
        resp = _proxy_request("GET", "/v1/models", api_key=api_key["key"])
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert isinstance(data["data"], list)

    def test_metrics_json_endpoint(self):
        resp = _proxy_request("GET", "/metrics/json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "pb-proxy"
        assert "uptime_seconds" in data


# ── Authentication ───────────────────────────────────────────

@pytest.mark.skipif(
    not _is_proxy_running() if os.getenv("RUN_INTEGRATION_TESTS") == "1" else True,
    reason="Proxy service not running",
)
class TestProxyAuth:
    """Proxy authentication tests."""

    def test_no_key_rejected(self):
        """Chat endpoint without API key should return 401."""
        resp = _proxy_request("POST", "/v1/chat/completions", json_body={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        })
        assert resp.status_code == 401

    def test_invalid_key_rejected(self):
        """Chat endpoint with invalid API key should return 401."""
        resp = _proxy_request("POST", "/v1/chat/completions",
            api_key="pb_invalid_key_12345",
            json_body={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 401

    def test_valid_key_passes_auth(self, api_key):
        """Valid API key should pass auth (may fail on LLM provider, but not 401)."""
        resp = _proxy_request("POST", "/v1/chat/completions",
            api_key=api_key["key"],
            json_body={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            timeout=30,
        )
        # Should not be 401 (auth passed), may be 4xx/5xx from LLM provider
        assert resp.status_code != 401


# ── OPA Policy Integration ───────────────────────────────────

@pytest.mark.skipif(
    not _is_proxy_running() if os.getenv("RUN_INTEGRATION_TESTS") == "1" else True,
    reason="Proxy service not running",
)
class TestProxyOPA:
    """Verify proxy OPA policy enforcement."""

    def test_viewer_denied_by_policy(self, wait_for_services):
        """Viewer role should be denied by OPA proxy policy."""
        import asyncio
        import asyncpg

        # Create a temporary viewer key
        loop = asyncio.new_event_loop()
        from conftest import _create_api_key, _delete_api_key
        viewer = loop.run_until_complete(_create_api_key("viewer"))

        try:
            resp = _proxy_request("POST", "/v1/chat/completions",
                api_key=viewer["key"],
                json_body={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            # Should be 403 (policy denied) — not 401 (auth failed)
            assert resp.status_code == 403
        finally:
            loop.run_until_complete(_delete_api_key(viewer["agent_id"]))
            loop.close()


# ── Tool Injection ───────────────────────────────────────────

@pytest.mark.skipif(
    not _is_proxy_running() if os.getenv("RUN_INTEGRATION_TESTS") == "1" else True,
    reason="Proxy service not running",
)
class TestProxyToolInjection:
    """Verify MCP tools are injected into proxy requests."""

    def test_health_shows_tools_loaded(self):
        """Health endpoint should report injected tools."""
        resp = _proxy_request("GET", "/health")
        data = resp.json()
        assert data["tools_loaded"] > 0, "No MCP tools loaded — is the MCP server running?"
