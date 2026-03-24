# E2E Smoke Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 8 self-contained E2E smoke tests that automatically start the Docker Compose stack, seed test data, verify critical paths (auth, search, policy, PII, graph), and tear down cleanly.

**Architecture:** Session-scoped pytest fixtures manage the full Docker lifecycle (up/down), health-waiting, Qdrant collection setup, Ollama model readiness, API key provisioning, and data seeding. Tests run against localhost ports and are gated behind `RUN_INTEGRATION_TESTS=1`.

**Tech Stack:** pytest, pytest-asyncio, httpx, asyncpg, subprocess (docker compose)

**Spec:** `docs/superpowers/specs/2026-03-24-e2e-smoke-tests-design.md`

---

### Task 1: Create E2E directory and conftest with Docker stack fixture

**Files:**
- Create: `tests/integration/e2e/__init__.py`
- Create: `tests/integration/e2e/conftest.py`

- [ ] **Step 1: Create the e2e package directory**

```bash
mkdir -p tests/integration/e2e
touch tests/integration/e2e/__init__.py
```

- [ ] **Step 2: Write the conftest.py with docker_stack and wait_for_services fixtures**

Create `tests/integration/e2e/conftest.py` with these fixtures:

```python
"""
E2E Smoke Test Fixtures.

Session-scoped fixtures that manage:
- Docker Compose stack lifecycle (up/down)
- Service health waiting
- Qdrant collection setup
- Ollama embedding model readiness
- API key provisioning (analyst + admin)
- Test data seeding via MCP ingest_data
"""

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import asyncpg
import httpx
import pytest

# ── Service URLs ─────────────────────────────────────────────

MCP_URL = os.getenv("MCP_URL", "http://localhost:8080/mcp")
INGESTION_URL = os.getenv("INGESTION_URL", "http://localhost:8081")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OPA_URL = os.getenv("OPA_URL", "http://localhost:8181")
RERANKER_URL = os.getenv("RERANKER_URL", "http://localhost:8082")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql://kb_admin:changeme@localhost:5432/knowledgebase",
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/e2e -> project root

HEALTH_ENDPOINTS = {
    "qdrant": f"{QDRANT_URL}/healthz",
    "opa": f"{OPA_URL}/health",
    "reranker": f"{RERANKER_URL}/health",
    "ollama": f"{OLLAMA_URL}/api/tags",
    "ingestion": f"{INGESTION_URL}/health",
}

QDRANT_COLLECTIONS = ["knowledge_general", "knowledge_code", "knowledge_rules"]

HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


# ── Helpers ──────────────────────────────────────────────────

def _compose(*args: str) -> subprocess.CompletedProcess:
    """Run a docker compose command in the project root."""
    cmd = ["docker", "compose", *args]
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _mcp_request(
    tool: str,
    arguments: dict,
    api_key: str,
    timeout: float = 10,
) -> httpx.Response:
    """Send a JSON-RPC tool call to the MCP server."""
    headers = {
        **HEADERS_BASE,
        "Authorization": f"Bearer {api_key}",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    return httpx.post(MCP_URL, json=body, headers=headers, timeout=timeout)


# ── Docker Stack ─────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def docker_stack():
    """Start Docker Compose stack, yield, then tear down with volumes."""
    # Clean slate
    _compose("down", "-v", "--remove-orphans")

    # Start stack
    result = _compose("up", "-d")
    if result.returncode != 0:
        pytest.fail(
            f"docker compose up failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    yield

    # Teardown: remove everything including volumes
    _compose("down", "-v", "--remove-orphans")


# ── Health Waiting ───────────────────────────────────────────

@pytest.fixture(scope="session")
def wait_for_services(docker_stack):
    """Wait until all services are healthy (max 120s)."""
    deadline = time.monotonic() + 120
    wait = 0.5
    pending = dict(HEALTH_ENDPOINTS)

    # MCP server doesn't have a simple GET health — we check TCP reachability
    # by attempting a POST later. For now just check the other services.

    while pending and time.monotonic() < deadline:
        still_pending = {}
        for name, url in pending.items():
            try:
                resp = httpx.get(url, timeout=5)
                if resp.status_code < 500:
                    continue  # healthy
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            still_pending[name] = url

        pending = still_pending
        if pending:
            time.sleep(min(wait, deadline - time.monotonic()))
            wait = min(wait * 2, 10)

    if pending:
        names = ", ".join(pending.keys())
        pytest.fail(f"Services not ready after 120s: {names}")

    # Also verify MCP server is reachable (POST-based)
    mcp_ok = False
    while time.monotonic() < deadline:
        try:
            # Use the dev key for a quick reachability check
            resp = httpx.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
                headers={
                    **HEADERS_BASE,
                    "Authorization": "Bearer kb_dev_localonly_do_not_use_in_production",
                },
                timeout=5,
            )
            if resp.status_code < 500:
                mcp_ok = True
                break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
            pass
        time.sleep(1)

    if not mcp_ok:
        pytest.fail("MCP server not reachable after 120s")


# ── Qdrant Collections ──────────────────────────────────────

@pytest.fixture(scope="session")
def setup_qdrant_collections(wait_for_services):
    """Create Qdrant collections if they don't exist."""
    for col in QDRANT_COLLECTIONS:
        try:
            resp = httpx.get(f"{QDRANT_URL}/collections/{col}", timeout=5)
            if resp.status_code == 200:
                continue
        except httpx.HTTPError:
            pass

        resp = httpx.put(
            f"{QDRANT_URL}/collections/{col}",
            json={"vectors": {"size": 768, "distance": "Cosine"}},
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            pytest.fail(f"Failed to create Qdrant collection '{col}': {resp.text}")


# ── Ollama Model ─────────────────────────────────────────────

@pytest.fixture(scope="session")
def ensure_embedding_model(wait_for_services):
    """Ensure nomic-embed-text model is available in Ollama."""
    model = "nomic-embed-text"
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            # Model names may include tag (e.g. "nomic-embed-text:latest")
            if any(model in m for m in models):
                return  # already available
    except httpx.HTTPError:
        pass

    # Pull the model via docker exec
    result = subprocess.run(
        ["docker", "exec", "kb-ollama", "ollama", "pull", model],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to pull Ollama model '{model}': {result.stderr}")


# ── API Keys ─────────────────────────────────────────────────

async def _create_api_key(role: str) -> dict:
    """Create a temporary API key in PostgreSQL."""
    key = "kb_test_" + secrets.token_hex(16)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    agent_id = f"e2e-{role}-{secrets.token_hex(4)}"

    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, agent_id, agent_role, description) "
            "VALUES ($1, $2, $3, $4)",
            key_hash, agent_id, role, f"E2E smoke test ({role})",
        )
    finally:
        await conn.close()

    return {"key": key, "agent_id": agent_id, "role": role}


async def _delete_api_key(agent_id: str):
    """Delete a temporary API key from PostgreSQL."""
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        await conn.execute("DELETE FROM api_keys WHERE agent_id = $1", agent_id)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def api_key(wait_for_services):
    """Session-scoped analyst API key."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    key_info = loop.run_until_complete(_create_api_key("analyst"))
    yield key_info
    loop.run_until_complete(_delete_api_key(key_info["agent_id"]))
    loop.close()


@pytest.fixture(scope="session")
def admin_api_key(wait_for_services):
    """Session-scoped admin API key."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    key_info = loop.run_until_complete(_create_api_key("admin"))
    yield key_info
    loop.run_until_complete(_delete_api_key(key_info["agent_id"]))
    loop.close()


# ── MCP Call Helper ──────────────────────────────────────────

@pytest.fixture(scope="session")
def mcp_call(api_key):
    """Return a helper function for MCP tool calls using the analyst key."""
    def _call(tool: str, arguments: dict, timeout: float = 10) -> httpx.Response:
        return _mcp_request(tool, arguments, api_key["key"], timeout=timeout)
    return _call


# ── Test Data Seeding ────────────────────────────────────────

TEST_DOCUMENTS = [
    {
        "id": "public-doc",
        "source": "Kubernetes orchestriert Container-Workloads und automatisiert Deployment, Skalierung und Management.",
        "classification": "public",
        "project": "e2e-test",
    },
    {
        "id": "pii-doc",
        "source": "Max Mustermann (max.mustermann@example.com) hat das Projekt 'Alpha' am 15.03.2026 abgeschlossen.",
        "classification": "internal",
        "project": "e2e-test",
    },
    {
        "id": "confidential-doc",
        "source": "Q4 Umsatz: 2.4M EUR. Geplante Expansion nach APAC in Q1 2027.",
        "classification": "confidential",
        "project": "e2e-test",
    },
]


@pytest.fixture(scope="session")
def seeded_data(api_key, setup_qdrant_collections, ensure_embedding_model):
    """Ingest test documents via MCP ingest_data tool. Returns document metadata."""
    results = {}
    for doc in TEST_DOCUMENTS:
        resp = _mcp_request(
            "ingest_data",
            {
                "source": doc["source"],
                "source_type": "text",
                "project": doc["project"],
                "classification": doc["classification"],
            },
            api_key["key"],
            timeout=30,
        )
        if resp.status_code != 200:
            pytest.fail(
                f"Failed to seed document '{doc['id']}': "
                f"status={resp.status_code}, body={resp.text}"
            )
        results[doc["id"]] = {**doc, "response": resp.json()}

    # Give Qdrant a moment to index
    time.sleep(2)

    return results
```

- [ ] **Step 3: Verify the conftest is syntactically correct**

Run: `python3 -c "import ast; ast.parse(open('tests/integration/e2e/conftest.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tests/integration/e2e/
git commit -m "feat(e2e): add conftest with Docker stack lifecycle and session fixtures"
```

---

### Task 2: Write the 8 smoke tests

**Files:**
- Create: `tests/integration/e2e/test_smoke.py`

- [ ] **Step 1: Write test_smoke.py with all 8 tests**

Create `tests/integration/e2e/test_smoke.py`:

```python
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
            # Pseudonym pattern should be present: [PERSON:hex]
            assert re.search(r"\[PERSON:[a-f0-9]+\]", result_text), (
                f"Expected pseudonym pattern [PERSON:<hash>] in results, got: {result_text[:500]}"
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
        assert "allowed" in result, f"Expected 'allowed' key in policy result: {result}"
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
```

- [ ] **Step 2: Verify test file is syntactically correct**

Run: `python3 -c "import ast; ast.parse(open('tests/integration/e2e/test_smoke.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Verify tests are collected (but skipped without RUN_INTEGRATION_TESTS)**

Run: `python3 -m pytest tests/integration/e2e/ --collect-only 2>&1 | head -20`
Expected: Tests collected, marked as skipped

- [ ] **Step 4: Commit**

```bash
git add tests/integration/e2e/test_smoke.py
git commit -m "feat(e2e): add 8 smoke tests for critical path verification"
```

---

### Task 3: Run the E2E tests and fix any issues

**Files:**
- Modify: `tests/integration/e2e/conftest.py` (if needed)
- Modify: `tests/integration/e2e/test_smoke.py` (if needed)

- [ ] **Step 1: Run the full E2E smoke test suite**

Run: `RUN_INTEGRATION_TESTS=1 python3 -m pytest tests/integration/e2e/ -v --tb=long 2>&1`

This will:
1. Start the Docker Compose stack
2. Wait for all services to be healthy
3. Set up Qdrant collections
4. Ensure the embedding model is available
5. Create API keys
6. Seed test data
7. Run all 8 tests
8. Tear down the stack

Expected: All 8 tests pass (or 8+ if health test is parametrized across services)

- [ ] **Step 2: Fix any failing tests**

If tests fail, analyze the output and fix:
- Timeout issues: increase timeouts
- Response format issues: adjust assertions to match actual JSON-RPC response structure
- Service startup order issues: add more wait time or retry logic
- PII detection issues: adjust expected pseudonym patterns

- [ ] **Step 3: Re-run to verify all tests pass**

Run: `RUN_INTEGRATION_TESTS=1 python3 -m pytest tests/integration/e2e/ -v --tb=short 2>&1`
Expected: All tests pass

- [ ] **Step 4: Commit fixes if any**

```bash
git add tests/integration/e2e/
git commit -m "fix(e2e): address issues found during initial test run"
```

---

### Task 4: Update documentation

**Files:**
- Modify: `CLAUDE.md` (add E2E test run command)

- [ ] **Step 1: Add E2E test instructions to CLAUDE.md**

In the `## Development` section, after the existing `### MCP Server Tests` subsection, add:

```markdown
### E2E Smoke Tests
```bash
# Full E2E (starts/stops Docker stack automatically)
RUN_INTEGRATION_TESTS=1 pytest tests/integration/e2e/ -v
```
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add E2E smoke test instructions to CLAUDE.md"
```
