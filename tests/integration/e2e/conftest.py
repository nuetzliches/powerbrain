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
_pg_password = os.getenv("PG_PASSWORD", "changeme_in_production")
POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    f"postgresql://kb_admin:{_pg_password}@localhost:5432/knowledgebase",
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

def _compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a docker compose command in the project root."""
    cmd = ["docker", "compose", *args]
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
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
    """Wait until all services are healthy (max 180s total)."""
    # Phase 1: wait for infrastructure services (120s)
    deadline = time.monotonic() + 120
    wait = 0.5
    pending = dict(HEALTH_ENDPOINTS)

    while pending and time.monotonic() < deadline:
        still_pending = {}
        for name, url in pending.items():
            try:
                resp = httpx.get(url, timeout=5)
                if resp.status_code < 500:
                    continue  # healthy
            except httpx.HTTPError:
                pass
            still_pending[name] = url

        pending = still_pending
        if pending:
            time.sleep(min(wait, max(0, deadline - time.monotonic())))
            wait = min(wait * 2, 10)

    if pending:
        names = ", ".join(pending.keys())
        pytest.fail(f"Services not ready after 120s: {names}")

    # Phase 2: wait for MCP server (separate 60s deadline)
    mcp_deadline = time.monotonic() + 60
    mcp_ok = False
    while time.monotonic() < mcp_deadline:
        try:
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
        except httpx.HTTPError:
            pass
        time.sleep(1)

    if not mcp_ok:
        pytest.fail("MCP server not reachable after 60s")


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
    loop = asyncio.new_event_loop()
    key_info = loop.run_until_complete(_create_api_key("analyst"))
    yield key_info
    loop.run_until_complete(_delete_api_key(key_info["agent_id"]))
    loop.close()


@pytest.fixture(scope="session")
def admin_api_key(wait_for_services):
    """Session-scoped admin API key."""
    loop = asyncio.new_event_loop()
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
