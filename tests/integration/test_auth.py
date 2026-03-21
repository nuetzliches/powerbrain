"""
Integration tests for API-Key authentication.
Requires: running MCP server + PostgreSQL (docker compose up).
"""

import hashlib
import json
import os
import secrets

import httpx
import asyncpg
import pytest

MCP_URL = os.getenv("MCP_URL", "http://localhost:8080/mcp")
POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql://kb_admin:changeme@localhost:5432/knowledgebase",
)

HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def mcp_request(tool: str, arguments: dict, headers: dict | None = None) -> httpx.Response:
    """Send a JSON-RPC tool call to the MCP server."""
    h = {**HEADERS_BASE, **(headers or {})}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    return httpx.post(MCP_URL, json=body, headers=h, timeout=10)


@pytest.fixture
async def test_api_key():
    """Create a temporary API key for testing, clean up after."""
    key = "kb_" + secrets.token_hex(32)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    agent_id = f"test-{secrets.token_hex(4)}"

    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, agent_id, agent_role, description) "
            "VALUES ($1, $2, $3, $4)",
            key_hash, agent_id, "analyst", "integration test key",
        )
        yield {"key": key, "agent_id": agent_id, "role": "analyst"}
    finally:
        await conn.execute("DELETE FROM api_keys WHERE agent_id = $1", agent_id)
        await conn.close()


class TestAuthRequired:
    """Tests with AUTH_REQUIRED=true (the default)."""

    def test_no_token_returns_401(self):
        resp = mcp_request("list_datasets", {})
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self):
        resp = mcp_request(
            "list_datasets", {},
            headers={"Authorization": "Bearer kb_invalid_key_here"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, test_api_key):
        resp = mcp_request(
            "list_datasets", {},
            headers={"Authorization": f"Bearer {test_api_key['key']}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should be a valid JSON-RPC response (no error at transport level)
        assert "jsonrpc" in data

    @pytest.mark.asyncio
    async def test_expired_key_returns_401(self):
        """An expired key should be rejected."""
        key = "kb_" + secrets.token_hex(32)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        agent_id = f"test-expired-{secrets.token_hex(4)}"

        conn = await asyncpg.connect(POSTGRES_URL)
        try:
            await conn.execute(
                "INSERT INTO api_keys (key_hash, agent_id, agent_role, expires_at) "
                "VALUES ($1, $2, $3, now() - interval '1 hour')",
                key_hash, agent_id, "analyst",
            )
            resp = mcp_request(
                "list_datasets", {},
                headers={"Authorization": f"Bearer {key}"},
            )
            assert resp.status_code == 401
        finally:
            await conn.execute("DELETE FROM api_keys WHERE agent_id = $1", agent_id)
            await conn.close()

    @pytest.mark.asyncio
    async def test_revoked_key_returns_401(self):
        """A revoked (active=false) key should be rejected."""
        key = "kb_" + secrets.token_hex(32)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        agent_id = f"test-revoked-{secrets.token_hex(4)}"

        conn = await asyncpg.connect(POSTGRES_URL)
        try:
            await conn.execute(
                "INSERT INTO api_keys (key_hash, agent_id, agent_role, active) "
                "VALUES ($1, $2, $3, false)",
                key_hash, agent_id, "analyst",
            )
            resp = mcp_request(
                "list_datasets", {},
                headers={"Authorization": f"Bearer {key}"},
            )
            assert resp.status_code == 401
        finally:
            await conn.execute("DELETE FROM api_keys WHERE agent_id = $1", agent_id)
            await conn.close()

    @pytest.mark.asyncio
    async def test_dev_key_works(self):
        """The default dev key should work out of the box."""
        resp = mcp_request(
            "list_datasets", {},
            headers={"Authorization": "Bearer kb_dev_localonly_do_not_use_in_production"},
        )
        assert resp.status_code == 200
