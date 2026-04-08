# Test Suite Design — Real Tests for Powerbrain

> **Approved:** 2026-03-21

## Goal

Replace the 21 structural "source-string-check" tests with real unit and integration tests. Cover all 4 services (mcp-server, ingestion, reranker, evaluation) with pytest, mocked I/O, and pure-function tests.

## Decisions

| Question | Decision | Reason |
|----------|----------|--------|
| Structural tests | Delete, replace with real tests | They don't verify any logic, only check whether strings appear in the source |
| Test scope | Everything: pure functions + mocked I/O + integration | Full coverage from the start |
| Framework | pytest + pytest-asyncio + pytest-mock + respx | Industry standard, async-native, existing tests already use pytest |
| Structure | Tests per service + central integration tests | Mirrors Docker architecture, trivial imports, no package restructuring |

## Directory structure

```
pyproject.toml                         # pytest configuration, markers, testpaths
requirements-dev.txt                   # Test dependencies

mcp-server/tests/
  conftest.py                          # Fixtures: mock httpx (respx), mock asyncpg pool
  test_validate_identifier.py          # graph_service: validate_identifier, _require_identifier, _escape_cypher_value
  test_token_validation.py             # server: validate_pii_access_token, redact_fields
  test_rate_limiter.py                 # server: TokenBucket
  test_embed_text.py                   # server: embed_text (mocked Ollama)
  test_opa_policy.py                   # server: check_opa_policy, filter_by_policy (mocked OPA)
  test_rerank.py                       # server: rerank_results incl. fallback (mocked Reranker)
  test_auth.py                         # server: ApiKeyVerifier (mocked asyncpg)
  test_log_access.py                   # server: log_access incl. PII scan fallback
  test_graph_crud.py                   # graph_service: create/find/delete node, relationships (mocked pool)

ingestion/tests/
  conftest.py                          # Fixtures: mock scanner, mock httpx
  test_chunk_text.py                   # ingestion_api: chunk_text (pure)
  test_pii_scanner.py                  # pii_scanner: scan/mask/pseudonymize (mocked Presidio)
  test_opa_privacy.py                  # ingestion_api: check_opa_privacy (mocked OPA)
  test_ingest_pipeline.py              # ingestion_api: ingest_text_chunks (all deps mocked)

reranker/tests/
  conftest.py                          # Fixture: mock CrossEncoder model
  test_rerank_endpoint.py              # service: /rerank (mocked model.predict)
  test_health.py                       # service: /health, /models

evaluation/tests/
  conftest.py                          # Fixtures: mock httpx
  test_metrics.py                      # run_eval: precision/recall/MRR/keyword_coverage (pure)
  test_search_with_opa.py              # run_eval: search + check_opa_access (mocked)

tests/integration/                     # Cross-service (existing tests stay)
  conftest.py
  test_auth.py                         # existing
  test_vault_integration.py            # existing
```

Old structural test files in the root `tests/` directory are deleted.

## Dependencies (requirements-dev.txt)

```
pytest>=8.0
pytest-asyncio>=0.24
pytest-mock>=3.14
respx>=0.22
coverage>=7.0
```

## pytest configuration (pyproject.toml)

```toml
[tool.pytest.ini_options]
testpaths = [
    "mcp-server/tests",
    "ingestion/tests",
    "reranker/tests",
    "evaluation/tests",
    "tests/integration",
]
asyncio_mode = "auto"
markers = [
    "integration: requires running services (deselect with -m 'not integration')",
]
```

## Fixture strategy

### mock_pg_pool (mcp-server, ingestion)

AsyncMock of the asyncpg.Pool interface. Return values configurable per test via `pool.fetchrow.return_value = {...}`.

```python
@pytest.fixture
def mock_pg_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    conn.fetch.return_value = []
    conn.fetchrow.return_value = None
    conn.execute.return_value = "INSERT 0 1"
    return pool
```

### mock_http (mcp-server, ingestion, evaluation)

respx router for httpx requests. Declarative mocking without monkeypatching.

```python
@pytest.fixture
def mock_http():
    with respx.mock(assert_all_called=False) as respx_mock:
        yield respx_mock
```

### mock_qdrant (mcp-server, ingestion)

AsyncMock of the QdrantClient with configurable search results.

## Test plan by priority

| Prio | File | Module | What is tested | Type |
|------|------|--------|----------------|------|
| 1 | `test_validate_identifier` | graph_service | SQL/Cypher injection prevention: valid identifiers, injection payloads, edge cases | Pure |
| 2 | `test_token_validation` | server | HMAC validation, expiry, invalid tokens, redact_fields with entity offsets | Pure |
| 3 | `test_rate_limiter` | server | TokenBucket consume/refill, burst, concurrency | Pure |
| 4 | `test_metrics` | run_eval | precision_at_k, recall_at_k, MRR, keyword_coverage: correct calculation, edge cases (empty, no hits) | Pure |
| 5 | `test_chunk_text` | ingestion_api | Chunking with overlap, short texts, exact boundaries | Pure |
| 6 | `test_embed_text` | server | Ollama call, retry on ConnectError/Timeout (tenacity), correct response parsing | Mocked |
| 7 | `test_opa_policy` | server | OPA allow/deny, fail-closed on error, filter_by_policy with asyncio.gather | Mocked |
| 8 | `test_rerank` | server | Reranker call, score sorting, graceful fallback on error | Mocked |
| 9 | `test_auth` | server | API key verification, role mapping, invalid keys, rate limit per agent | Mocked |
| 10 | `test_graph_crud` | graph_service | Cypher generation, AGE agtype parsing, error handling | Mocked |
| 11 | `test_pii_scanner` | pii_scanner | Scan/mask/pseudonymize, deterministic hashing, multi-entity | Mocked |
| 12 | `test_rerank_endpoint` | reranker/service | FastAPI /rerank: sorting, top_n, empty docs, max_batch | Mocked |
| 13 | `test_log_access` | server | Audit log insert, PII scan fallback on error | Mocked |
| 14 | `test_ingest_pipeline` | ingestion_api | Full flow: chunk -> scan -> OPA -> embed -> store (all deps mocked) | Mocked |
| 15 | `test_search_with_opa` | run_eval | Eval search with OPA filter, cache behavior | Mocked |

## Execution

```bash
# All unit tests (no running service needed)
pytest -m "not integration" -v

# Test a single service only
pytest mcp-server/tests/ -v

# With coverage
pytest --cov=mcp-server --cov=ingestion --cov=reranker --cov=evaluation -m "not integration"

# Integration tests (services must be running)
RUN_INTEGRATION_TESTS=1 pytest tests/integration/ -v
```

## What gets deleted

21 structural test files in the root `tests/`:
- test_agtype_parsing.py, test_art17_vault_deletion.py, test_audit_pii_protection.py
- test_eval_opa_filter.py, test_find_path_fallback.py, test_graph_sync_log.py
- test_ingestion_cleanup.py, test_ingestion_dual_storage.py, test_injection_prevention.py
- test_list_datasets_source_type.py, test_mcp_requirements.py, test_mcp_vault_access.py
- test_opa_privacy_extensions.py, test_parallel_opa_checks.py, test_pg_pool_lifespan.py
- test_pii_scanner_config.py, test_pii_vault_schema.py, test_pseudonymize_fix.py
- test_rate_limiter.py, test_retention_vault_cleanup.py, test_retry_config.py
- test_search_first_mvp_docs.py, test_search_first_mvp_scripts.py
- test_search_first_mvp_structure.py

Kept: `tests/integration/test_auth.py`, `tests/integration/test_vault_integration.py` (moved from tests/).
