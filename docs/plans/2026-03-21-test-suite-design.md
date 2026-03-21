# Test-Suite Design — Echte Tests für Powerbrain

> **Approved:** 2026-03-21

## Ziel

Die 21 strukturellen "Source-String-Check"-Tests durch echte Unit- und Integration-Tests ersetzen. Alle 4 Services (mcp-server, ingestion, reranker, evaluation) abdecken mit pytest, mocked I/O und pure-function Tests.

## Entscheidungen

| Frage | Entscheidung | Grund |
|-------|-------------|-------|
| Strukturelle Tests | Löschen, durch echte Tests ersetzen | Prüfen keine Logik, nur ob Strings im Source vorkommen |
| Test-Scope | Alles: Pure Functions + Mocked I/O + Integration | Vollständige Abdeckung von Anfang an |
| Framework | pytest + pytest-asyncio + pytest-mock + respx | Industriestandard, async-native, bestehende Tests nutzen bereits pytest |
| Struktur | Tests pro Service + zentrale Integration-Tests | Spiegelt Docker-Architektur, triviale Imports, kein Package-Umbau |

## Verzeichnisstruktur

```
pyproject.toml                         # pytest-Konfiguration, Markers, testpaths
requirements-dev.txt                   # Test-Dependencies

mcp-server/tests/
  conftest.py                          # Fixtures: mock httpx (respx), mock asyncpg pool
  test_validate_identifier.py          # graph_service: validate_identifier, _require_identifier, _escape_cypher_value
  test_token_validation.py             # server: validate_pii_access_token, redact_fields
  test_rate_limiter.py                 # server: TokenBucket
  test_embed_text.py                   # server: embed_text (mocked Ollama)
  test_opa_policy.py                   # server: check_opa_policy, filter_by_policy (mocked OPA)
  test_rerank.py                       # server: rerank_results inkl. Fallback (mocked Reranker)
  test_auth.py                         # server: ApiKeyVerifier (mocked asyncpg)
  test_log_access.py                   # server: log_access inkl. PII-Scan-Fallback
  test_graph_crud.py                   # graph_service: create/find/delete node, relationships (mocked pool)

ingestion/tests/
  conftest.py                          # Fixtures: mock scanner, mock httpx
  test_chunk_text.py                   # ingestion_api: chunk_text (pure)
  test_pii_scanner.py                  # pii_scanner: scan/mask/pseudonymize (mocked Presidio)
  test_opa_privacy.py                  # ingestion_api: check_opa_privacy (mocked OPA)
  test_ingest_pipeline.py              # ingestion_api: ingest_text_chunks (alle Deps gemockt)

reranker/tests/
  conftest.py                          # Fixture: mock CrossEncoder model
  test_rerank_endpoint.py              # service: /rerank (mocked model.predict)
  test_health.py                       # service: /health, /models

evaluation/tests/
  conftest.py                          # Fixtures: mock httpx
  test_metrics.py                      # run_eval: precision/recall/MRR/keyword_coverage (pure)
  test_search_with_opa.py              # run_eval: search + check_opa_access (mocked)

tests/integration/                     # Cross-Service (bestehende Tests bleiben)
  conftest.py
  test_auth.py                         # bestehend
  test_vault_integration.py            # bestehend
```

Alte strukturelle Testdateien im Root `tests/` werden gelöscht.

## Dependencies (requirements-dev.txt)

```
pytest>=8.0
pytest-asyncio>=0.24
pytest-mock>=3.14
respx>=0.22
coverage>=7.0
```

## pytest-Konfiguration (pyproject.toml)

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

## Fixture-Strategie

### mock_pg_pool (mcp-server, ingestion)

AsyncMock der asyncpg.Pool-Schnittstelle. Return-Werte pro Test konfigurierbar via `pool.fetchrow.return_value = {...}`.

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

respx-Router für httpx-Requests. Deklaratives Mocking ohne monkeypatching.

```python
@pytest.fixture
def mock_http():
    with respx.mock(assert_all_called=False) as respx_mock:
        yield respx_mock
```

### mock_qdrant (mcp-server, ingestion)

AsyncMock des QdrantClient mit konfigurierbaren Search-Results.

## Testplan nach Priorität

| Prio | Datei | Modul | Was wird getestet | Typ |
|------|-------|-------|-------------------|-----|
| 1 | `test_validate_identifier` | graph_service | SQL/Cypher-Injection-Prevention: valid identifiers, injection payloads, edge cases | Pure |
| 2 | `test_token_validation` | server | HMAC-Validierung, Ablauf, ungültige Tokens, redact_fields mit Entity-Offsets | Pure |
| 3 | `test_rate_limiter` | server | TokenBucket consume/refill, Burst, Concurrency | Pure |
| 4 | `test_metrics` | run_eval | precision_at_k, recall_at_k, MRR, keyword_coverage: korrekte Berechnung, Edge Cases (leer, keine Treffer) | Pure |
| 5 | `test_chunk_text` | ingestion_api | Chunking mit Overlap, kurze Texte, exakte Grenzen | Pure |
| 6 | `test_embed_text` | server | Ollama-Aufruf, Retry bei ConnectError/Timeout (tenacity), korrektes Response-Parsing | Mocked |
| 7 | `test_opa_policy` | server | OPA allow/deny, Fail-closed bei Fehler, filter_by_policy mit asyncio.gather | Mocked |
| 8 | `test_rerank` | server | Reranker-Aufruf, Score-Sortierung, Graceful Fallback bei Fehler | Mocked |
| 9 | `test_auth` | server | API-Key-Verifikation, Rollen-Mapping, ungültige Keys, Rate-Limit pro Agent | Mocked |
| 10 | `test_graph_crud` | graph_service | Cypher-Generierung, AGE agtype-Parsing, Fehlerbehandlung | Mocked |
| 11 | `test_pii_scanner` | pii_scanner | Scan/Mask/Pseudonymize, deterministisches Hashing, Multi-Entity | Mocked |
| 12 | `test_rerank_endpoint` | reranker/service | FastAPI /rerank: Sortierung, top_n, leere Docs, max_batch | Mocked |
| 13 | `test_log_access` | server | Audit-Log-Insert, PII-Scan-Fallback bei Fehler | Mocked |
| 14 | `test_ingest_pipeline` | ingestion_api | Full Flow: chunk -> scan -> OPA -> embed -> store (alle Deps gemockt) | Mocked |
| 15 | `test_search_with_opa` | run_eval | Eval-Search mit OPA-Filter, Cache-Verhalten | Mocked |

## Ausführung

```bash
# Alle Unit-Tests (kein laufender Service nötig)
pytest -m "not integration" -v

# Nur einen Service testen
pytest mcp-server/tests/ -v

# Mit Coverage
pytest --cov=mcp-server --cov=ingestion --cov=reranker --cov=evaluation -m "not integration"

# Integration-Tests (Services müssen laufen)
RUN_INTEGRATION_TESTS=1 pytest tests/integration/ -v
```

## Was gelöscht wird

21 strukturelle Testdateien im Root `tests/`:
- test_agtype_parsing.py, test_art17_vault_deletion.py, test_audit_pii_protection.py
- test_eval_opa_filter.py, test_find_path_fallback.py, test_graph_sync_log.py
- test_ingestion_cleanup.py, test_ingestion_dual_storage.py, test_injection_prevention.py
- test_list_datasets_source_type.py, test_mcp_requirements.py, test_mcp_vault_access.py
- test_opa_privacy_extensions.py, test_parallel_opa_checks.py, test_pg_pool_lifespan.py
- test_pii_scanner_config.py, test_pii_vault_schema.py, test_pseudonymize_fix.py
- test_rate_limiter.py, test_retention_vault_cleanup.py, test_retry_config.py
- test_search_first_mvp_docs.py, test_search_first_mvp_scripts.py
- test_search_first_mvp_structure.py

Behalten werden: `tests/integration/test_auth.py`, `tests/integration/test_vault_integration.py` (verschoben aus tests/).
