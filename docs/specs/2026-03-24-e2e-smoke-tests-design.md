# E2E Smoke Tests Design

**Date:** 2026-03-24
**Status:** Approved
**Goal:** Verify critical paths through the full Powerbrain stack with 8 fast, self-contained smoke tests.

## Context

Powerbrain has 32 test files across 6 components, all unit/component-level with mocked dependencies. Three integration tests exist (`tests/integration/`) but are narrow and point-specific. No automated E2E test covers the critical ingest-search-policy pipeline end-to-end.

## Requirements

- **Smoke scope:** ~8 tests covering critical happy paths, not exhaustive coverage
- **Automatic stack:** Tests start the Docker Compose stack, run, then tear it down (clean state)
- **Seed + verify:** Tests ingest their own data, then query and verify results
- **Self-contained:** No manual setup required — just `RUN_INTEGRATION_TESTS=1 pytest tests/integration/e2e/ -v`
- **Clean state:** `docker compose down -v` before and after — no leftover data between runs

## File Structure

```
tests/integration/
├── conftest.py                  ← Existing RUN_INTEGRATION_TESTS gate (unchanged)
├── test_auth.py                 ← Existing (unchanged)
├── test_vault_integration.py    ← Existing (unchanged)
├── test_pii_chat_protection.py  ← Existing (unchanged)
├── e2e/
│   ├── conftest.py              ← NEW: Session fixtures (stack lifecycle, health-wait, seed, API keys)
│   └── test_smoke.py            ← NEW: 8 smoke tests
```

E2E tests inherit the `RUN_INTEGRATION_TESTS=1` gate from the parent conftest. The `e2e/conftest.py` provides all session-scoped fixtures.

## Session Fixtures (`e2e/conftest.py`)

### Fixture Dependency Chain

```
docker_stack (autouse, session)
  └── wait_for_services (session, 120s timeout)
       ├── setup_qdrant_collections (session)
       ├── ensure_embedding_model (session)
       └── api_key / admin_api_key (session)
            └── seeded_data (session)
                 └── mcp_call (session, helper)
```

### Fixture Details

| Fixture | Scope | Purpose |
|---|---|---|
| `docker_stack` | session, autouse | **Setup:** `docker compose down -v` (clean slate), then `docker compose up -d`. **Teardown:** `docker compose down -v` (remove all volumes). Uses `subprocess.run` with project root as cwd. |
| `wait_for_services` | session | HTTP healthcheck against all 6 services. Max **120s** total with exponential backoff (0.5s, 1s, 2s, 4s, ...). Fails with clear error naming which service is unreachable. |
| `setup_qdrant_collections` | session | Creates Qdrant collections (`knowledge_general`, `knowledge_code`, `knowledge_rules`) via REST API if they don't exist. Depends on `wait_for_services`. |
| `ensure_embedding_model` | session | Checks if `nomic-embed-text` model is available in Ollama. If not, pulls it via `docker exec kb-ollama ollama pull nomic-embed-text`. Depends on `wait_for_services`. |
| `api_key` | session | Creates a temporary API key in PostgreSQL (`kb_test_...`, role `analyst`). **Cleanup:** `DELETE FROM api_keys WHERE agent_id = $1` via asyncpg. Depends on `wait_for_services`. |
| `admin_api_key` | session | Same as above but with role `admin`. **Cleanup:** same DELETE. For tests that require elevated access. |
| `seeded_data` | session | Ingests 3 test documents via MCP `ingest_data` tool (one public, one internal with PII, one confidential). Returns metadata for verification. Depends on `api_key` + `ensure_embedding_model` + `setup_qdrant_collections`. |
| `mcp_call` | session | Helper function that sends JSON-RPC calls to the MCP server with correct auth header. Format: `{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "<tool>", "arguments": {…}}}`. Headers: `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `Authorization: Bearer <key>`. Returns `httpx.Response`. |

## Test Scenarios (`test_smoke.py`)

### Health & Basics

1. **`test_all_services_healthy`** — All services respond to health endpoints:
   - Qdrant: `GET /healthz` -> 200
   - OPA: `GET /health` -> 200
   - Reranker: `GET /health` -> 200
   - Ollama: `GET /api/tags` -> 200
   - MCP Server: reachable on port 8080
   - Ingestion: `GET /health` -> 200

2. **`test_auth_valid_key`** — MCP `list_datasets` call with valid API key -> 200, valid JSON-RPC response

3. **`test_auth_no_key_rejected`** — MCP `list_datasets` call without auth header -> 401

### Ingest -> Search Pipeline

4. **`test_search_finds_seeded_data`** — `search_knowledge` with a query matching the seeded public document -> at least 1 result containing expected content

5. **`test_search_policy_blocks_confidential`** — Analyst calls `search_knowledge` -> `confidential`-classified document does NOT appear in results

### PII & Vault

6. **`test_pii_data_pseudonymized_in_search`** — Search for PII document -> result text contains typed pseudonyms in format `[PERSON:<hash>]` (deterministic, 8-char hex), NOT the original name "Max Mustermann" or email "max.mustermann@example.com"

### OPA Policy

7. **`test_check_policy_evaluates`** — `check_policy` tool with known input (e.g., `analyst` reading `internal` data) -> expected allow/deny response

### Knowledge Graph

8. **`test_graph_query_returns_result`** — `graph_query` tool with query `{"type": "nodes", "label": "Document"}` -> valid response (no error; may return empty list if graph is unpopulated, but must not return an error)

## Error Handling

- Each test has a 10s per-request timeout
- `wait_for_services` has a **120s** total timeout with exponential backoff
- On timeout: clear error message naming which service is missing
- Seeding failures: `pytest.fail` with explanation (infrastructure problem)
- Stack start failures: `pytest.fail` with docker compose output
- All fixtures use try/finally for cleanup

## Service Endpoints

| Service | Health URL | Port |
|---|---|---|
| Qdrant | `http://localhost:6333/healthz` | 6333 |
| OPA | `http://localhost:8181/health` | 8181 |
| Reranker | `http://localhost:8082/health` | 8082 |
| Ollama | `http://localhost:11434/api/tags` | 11434 |
| MCP Server | `http://localhost:8080/mcp` | 8080 |
| Ingestion | `http://localhost:8081/health` | 8081 |

## Execution

```bash
# Fully automatic — starts stack, tests, tears down
RUN_INTEGRATION_TESTS=1 pytest tests/integration/e2e/ -v

# Or run all integration tests together (existing ones still need manual stack)
RUN_INTEGRATION_TESTS=1 pytest tests/integration/ -v
```

No manual `docker compose up` required. The test session handles the full lifecycle.

## Test Data

Seeded via MCP `ingest_data` tool after stack is ready:

1. **Public document (no PII):**
   - Text: "Kubernetes orchestriert Container-Workloads und automatisiert Deployment, Skalierung und Management."
   - Classification: `public`
   - Project: `e2e-test`

2. **Internal document (with PII):**
   - Text: "Max Mustermann (max.mustermann@example.com) hat das Projekt 'Alpha' am 15.03.2026 abgeschlossen."
   - Classification: `internal`
   - Project: `e2e-test`

3. **Confidential document:**
   - Text: "Q4 Umsatz: 2.4M EUR. Geplante Expansion nach APAC in Q1 2027."
   - Classification: `confidential`
   - Project: `e2e-test`

## Stack Lifecycle Timing

| Phase | Expected Duration |
|---|---|
| `docker compose down -v` (cleanup) | ~5s |
| `docker compose up -d` (start) | ~10-15s |
| Health wait (all services ready) | ~15-30s |
| Ollama model pull (first run only) | ~30-60s |
| Qdrant collection setup | ~2s |
| Data seeding (3 documents) | ~5-10s |
| 8 smoke tests | ~15-20s |
| `docker compose down -v` (teardown) | ~5s |
| **Total (first run)** | **~90-150s** |
| **Total (subsequent runs)** | **~60-90s** |

## Non-Goals

- No proxy (`pb-proxy`) tests — these are a separate concern
- No performance/load testing
- No TLS testing
- No Forgejo integration testing
- No monitoring/metrics verification
