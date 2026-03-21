"""
Integration test for the proxy.
Requires: mcp-server, opa running (skip if not available).
"""

import os
import pytest
import httpx

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8090")
MCP_URL = os.getenv("MCP_URL", "http://localhost:8080")


def is_service_running(url: str) -> bool:
    try:
        resp = httpx.get(f"{url}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(
    not is_service_running(os.getenv("PROXY_URL", "http://localhost:8090")),
    reason="Proxy service not running",
)
class TestProxyIntegration:

    def test_health(self):
        resp = httpx.get(f"{PROXY_URL}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["tools_loaded"] > 0

    def test_chat_completions_without_api_key(self):
        """Request without LLM API key should fail gracefully."""
        resp = httpx.post(
            f"{PROXY_URL}/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            timeout=30,
        )
        # Should fail with 502 (LLM provider error) not 500 (server crash)
        assert resp.status_code in (502, 403)
