"""
E2E Smoke Tests for Powerbrain.

Verifies critical paths through the full stack:
- Service health
- Authentication (valid/invalid)
- Ingest -> Search pipeline
- OPA policy enforcement (confidential blocked for analyst)
- PII pseudonymization in search results
- OPA check_policy tool
- Knowledge graph query

Requires: RUN_INTEGRATION_TESTS=1
Stack is managed automatically by session fixtures in conftest.py.
"""

import json
import re

import httpx
import pytest

from .conftest import (
    HEALTH_ENDPOINTS,
    MCP_URL,
    HEADERS_BASE,
    _mcp_request,
)


# ── 1. Health & Basics ───────────────────────────────────────


class TestHealth:
    """Verify all services are healthy."""

    @pytest.mark.parametrize("service,url", list(HEALTH_ENDPOINTS.items()))
    def test_all_services_healthy(self, wait_for_services, service, url):
        """Each service responds to its health endpoint."""
        resp = httpx.get(url, timeout=10)
        assert resp.status_code < 500, f"{service} unhealthy: {resp.status_code}"


class TestAuth:
    """Verify authentication works correctly."""

    def test_auth_valid_key(self, mcp_call):
        """Valid API key should be accepted."""
        resp = mcp_call("list_datasets", {})
        assert resp.status_code == 200
        data = resp.json()
        assert "jsonrpc" in data, f"Unexpected response: {data}"

    def test_auth_no_key_rejected(self, wait_for_services):
        """Missing API key should be rejected with 401."""
        resp = httpx.post(
            MCP_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_datasets", "arguments": {}},
            },
            headers=HEADERS_BASE,
            timeout=10,
        )
        assert resp.status_code == 401


# ── 2. Ingest -> Search Pipeline ─────────────────────────────


class TestSearchPipeline:
    """Verify the ingest -> embed -> search -> rerank pipeline."""

    def test_search_finds_seeded_data(self, mcp_call, seeded_data):
        """Search should find the seeded public document."""
        resp = mcp_call(
            "search_knowledge",
            {"query": "Kubernetes Container Deployment"},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()

        # Extract the text content from JSON-RPC response
        result_text = ""
        if "result" in data:
            for content in data["result"].get("content", []):
                if content.get("type") == "text":
                    result_text += content["text"]

        assert "Kubernetes" in result_text or "Container" in result_text, (
            f"Expected seeded document in results, got: {result_text[:500]}"
        )

    def test_search_policy_blocks_confidential(self, mcp_call, seeded_data):
        """Analyst should NOT see confidential documents in search results."""
        resp = mcp_call(
            "search_knowledge",
            {"query": "Umsatz Expansion APAC"},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()

        result_text = ""
        if "result" in data:
            for content in data["result"].get("content", []):
                if content.get("type") == "text":
                    result_text += content["text"]

        # The confidential document content should NOT appear
        assert "2.4M EUR" not in result_text, (
            "Confidential document leaked to analyst role"
        )


# ── 3. PII & Vault ───────────────────────────────────────────


class TestPII:
    """Verify PII pseudonymization in search results."""

    def test_pii_data_pseudonymized_in_search(self, mcp_call, seeded_data):
        """PII document should return pseudonymized text, not originals."""
        resp = mcp_call(
            "search_knowledge",
            {"query": "Projekt Alpha abgeschlossen"},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()

        result_text = ""
        if "result" in data:
            for content in data["result"].get("content", []):
                if content.get("type") == "text":
                    result_text += content["text"]

        if result_text:
            # Original PII must NOT appear
            assert "Max Mustermann" not in result_text, (
                "Original PII name found in search results"
            )
            assert "max.mustermann@example.com" not in result_text, (
                "Original PII email found in search results"
            )
            # Pseudonym pattern: <PERSON> or [PERSON:<hash>] (Presidio-style)
            has_pseudonym = (
                "<PERSON>" in result_text
                or re.search(r"\[PERSON:[a-f0-9]+\]", result_text)
            )
            # If the PII document was returned, it must contain pseudonyms
            if "Projekt" in result_text or "Alpha" in result_text:
                assert has_pseudonym, (
                    f"Expected pseudonym pattern in PII document results, "
                    f"got: {result_text[:500]}"
                )


# ── 4. OPA Policy ────────────────────────────────────────────


class TestPolicy:
    """Verify OPA policy evaluation."""

    def test_check_policy_evaluates(self, mcp_call):
        """check_policy should return a valid allow/deny decision."""
        resp = mcp_call(
            "check_policy",
            {
                "action": "read",
                "resource": "dataset/test",
                "classification": "internal",
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        # Extract result text
        result_text = ""
        if "result" in data:
            for content in data["result"].get("content", []):
                if content.get("type") == "text":
                    result_text += content["text"]

        result = json.loads(result_text)
        assert "allowed" in result, (
            f"Expected 'allowed' key in policy result: {result}"
        )
        # Analyst reading internal data should be allowed
        assert result["allowed"] is True


# ── 5. Knowledge Graph ───────────────────────────────────────


class TestGraph:
    """Verify knowledge graph queries work."""

    def test_graph_query_returns_result(self, mcp_call):
        """graph_query with find_node should return a valid response."""
        resp = mcp_call(
            "graph_query",
            {"action": "find_node", "label": "Document"},
        )
        assert resp.status_code == 200
        data = resp.json()

        result_text = ""
        if "result" in data:
            for content in data["result"].get("content", []):
                if content.get("type") == "text":
                    result_text += content["text"]

        result = json.loads(result_text)
        # Should not contain an error (empty results are fine)
        assert "error" not in result, f"Graph query returned error: {result}"
