# kb → pb Prefix Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename all project-specific `kb` prefixes to `pb` (Powerbrain) across the entire codebase for consistent branding.

**Architecture:** Mechanical find-and-replace organized by component. Big-bang migration — requires `docker compose down -v` and fresh setup after completion. No backward-compatibility layer needed.

**Tech Stack:** Docker Compose, Python (FastAPI), OPA (Rego), PostgreSQL, Qdrant, Prometheus/Grafana, JSON

**Rename mapping:**

| Category | Old | New |
|---|---|---|
| Container prefix | `kb-` | `pb-` |
| Docker network | `kb-net` | `pb-net` |
| API token prefix | `kb_` | `pb_` |
| OPA packages | `package kb.*` | `package pb.*` |
| OPA policy dir | `opa-policies/kb/` | `opa-policies/pb/` |
| OPA data paths | `/v1/data/kb/` | `/v1/data/pb/` |
| DB user | `kb_admin` | `pb_admin` |
| DB name | `knowledgebase` | `powerbrain` |
| Prometheus metrics | `kb_*` | `pb_*` |
| Qdrant collections | `knowledge_general`, `knowledge_code`, `knowledge_rules` | `pb_general`, `pb_code`, `pb_rules` |
| Forgejo org/repos | `kb-org/kb-*` | `pb-org/pb-*` |
| API titles | `KB *` | `Powerbrain *` |
| Logger names | `kb-*` | `pb-*` |
| MCP server name | `kb-mcp-server` | `pb-mcp-server` |
| Dashboard files | `kb-*.json` | `pb-*.json` |
| Dev token | `kb_dev_localonly_...` | `pb_dev_localonly_...` |

---

### Task 1: Docker Compose — Container Names, Network, Env Vars

**Files:**
- Modify: `docker-compose.yml`

All changes are in a single file. Apply the following replacements:

- [ ] **Step 1: Rename Docker network**

Replace `kb-net` → `pb-net` (all occurrences — network definition + every service's `networks:` block).

- [ ] **Step 2: Rename all container names**

Replace all `container_name: kb-` → `container_name: pb-` (16 containers):
- `kb-qdrant` → `pb-qdrant`
- `kb-postgres` → `pb-postgres`
- `kb-postgres-exporter` → `pb-postgres-exporter`
- `kb-opa` → `pb-opa`
- `kb-ollama` → `pb-ollama`
- `kb-reranker` → `pb-reranker`
- `kb-mcp-server` → `pb-mcp-server`
- `kb-ingestion` → `pb-ingestion`
- `kb-prometheus` → `pb-prometheus`
- `kb-grafana` → `pb-grafana`
- `kb-tempo` → `pb-tempo`
- `kb-seed` → `pb-seed`
- `kb-proxy` → `pb-proxy`
- `kb-vllm` → `pb-vllm`
- `kb-tei` → `pb-tei`
- `kb-caddy` → `pb-caddy`

- [ ] **Step 3: Rename DB user and DB name in env vars and connection strings**

- `POSTGRES_USER: kb_admin` → `POSTGRES_USER: pb_admin`
- `POSTGRES_DB: knowledgebase` → `POSTGRES_DB: powerbrain`
- `pg_isready -U kb_admin -d knowledgebase` → `pg_isready -U pb_admin -d powerbrain`
- All `postgresql://kb_admin:...@.../knowledgebase` → `postgresql://pb_admin:...@.../powerbrain` (lines 89, 183, 230)

- [ ] **Step 4: Rename dev token**

- `MCP_AUTH_TOKEN: kb_dev_localonly_do_not_use_in_production` → `MCP_AUTH_TOKEN: pb_dev_localonly_do_not_use_in_production`

- [ ] **Step 5: Rename OPA bundle config (commented out)**

- `bundles.kb.` → `bundles.pb.` (lines 114-116)
- `kb-org/kb-policies` → `pb-org/pb-policies`

- [ ] **Step 6: Verify docker-compose.yml has no remaining `kb` references**

Run: `grep -n "kb" docker-compose.yml` — should return nothing.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml
git commit -m "refactor: rename kb → pb prefix in docker-compose.yml"
```

---

### Task 2: OPA Policies — Directory, Packages, Imports

**Files:**
- Rename directory: `opa-policies/kb/` → `opa-policies/pb/`
- Modify all `.rego` files inside (9 files)

- [ ] **Step 1: Rename OPA policy directory**

```bash
git mv opa-policies/kb opa-policies/pb
```

- [ ] **Step 2: Rename packages in all policy files**

In each `.rego` file, replace `package kb.` → `package pb.`:
- `opa-policies/pb/access.rego`: `package kb.access` → `package pb.access`
- `opa-policies/pb/layers.rego`: `package kb.layers` → `package pb.layers`
- `opa-policies/pb/privacy.rego`: `package kb.privacy` → `package pb.privacy`
- `opa-policies/pb/proxy.rego`: `package kb.proxy` → `package pb.proxy`
- `opa-policies/pb/rules.rego`: `package kb.rules` → `package pb.rules`
- `opa-policies/pb/summarization.rego`: `package kb.summarization` → `package pb.summarization`

- [ ] **Step 3: Rename test packages and imports**

- `opa-policies/pb/test_layers.rego`: `package kb.layers_test` → `package pb.layers_test`, `import data.kb.layers` → `import data.pb.layers`
- `opa-policies/pb/test_proxy.rego`: `package kb.proxy_test` → `package pb.proxy_test`, `import data.kb.proxy` → `import data.pb.proxy`
- `opa-policies/pb/test_summarization.rego`: `package kb.summarization_test` → `package pb.summarization_test`, `import data.kb.summarization` → `import data.pb.summarization`

- [ ] **Step 4: Verify no remaining `kb` in rego files**

Run: `grep -rn "kb" opa-policies/pb/` — should return nothing.

- [ ] **Step 5: Run OPA tests to verify**

Run: `docker run --rm -v $(pwd)/opa-policies/pb:/policies/pb openpolicyagent/opa:latest test /policies/pb/ -v`

Note: If docker is not available in the agent, skip this step and note it for manual verification.

- [ ] **Step 6: Commit**

```bash
git add opa-policies/
git commit -m "refactor: rename OPA policy packages from kb.* to pb.*"
```

---

### Task 3: SQL Init Scripts — DB User, Policy Paths, Collection Names

**Files:**
- Modify: `init-db/001_schema.sql`
- Modify: `init-db/004_evaluation.sql`
- Modify: `init-db/005_versioning.sql`
- Modify: `init-db/007_pii_vault.sql`
- Modify: `init-db/008_audit_rls.sql`
- Modify: `init-db/010_api_keys.sql`

- [ ] **Step 1: Rename OPA policy paths in 001_schema.sql**

Replace all `kb.access.` → `pb.access.` (lines 15-18):
```sql
('public',       0, 'Frei zugänglich für alle Agenten', 'pb.access.public'),
('internal',     1, 'Nur für interne Agenten',          'pb.access.internal'),
('confidential', 2, 'Eingeschränkter Zugriff',          'pb.access.confidential'),
('restricted',   3, 'Streng kontrolliert',               'pb.access.restricted');
```

- [ ] **Step 2: Rename collection default in 004_evaluation.sql**

Replace `DEFAULT 'knowledge_general'` → `DEFAULT 'pb_general'` (line 32).

- [ ] **Step 3: Update comment in 005_versioning.sql**

Replace `knowledge_general` → `pb_general` in the JSON comment (line 15).

- [ ] **Step 4: Update comments in 007_pii_vault.sql**

Replace `kb_admin` → `pb_admin` in comments (lines 17, 90).

- [ ] **Step 5: Update 008_audit_rls.sql**

Replace `knowledgebase` → `powerbrain` in `GRANT CONNECT ON DATABASE` (line 23).

- [ ] **Step 6: Update default key hash in 010_api_keys.sql**

Replace the hash for `kb_dev_localonly_do_not_use_in_production` with the hash for `pb_dev_localonly_do_not_use_in_production`.

To compute the new hash:
```python
import hashlib
hashlib.sha256("pb_dev_localonly_do_not_use_in_production".encode()).hexdigest()
```

Also update the comment/description if it references the old token.

- [ ] **Step 7: Verify no remaining `kb` in SQL files**

Run: `grep -rn "kb" init-db/` — should return nothing.

- [ ] **Step 8: Commit**

```bash
git add init-db/
git commit -m "refactor: rename kb → pb in SQL init scripts"
```

---

### Task 4: MCP Server — Metrics, Logger, Server Name, OPA Paths, Collections

**Files:**
- Modify: `mcp-server/server.py`
- Modify: `mcp-server/graph_service.py`
- Modify: `mcp-server/manage_keys.py`

- [ ] **Step 1: Rename Prometheus metrics in server.py**

Replace all metric name prefixes (lines 114-147):
- `kb_mcp_requests_total` → `pb_mcp_requests_total`
- `kb_mcp_request_duration_seconds` → `pb_mcp_request_duration_seconds`
- `kb_mcp_policy_decisions_total` → `pb_mcp_policy_decisions_total`
- `kb_mcp_search_results_count` → `pb_mcp_search_results_count`
- `kb_mcp_rerank_fallback_total` → `pb_mcp_rerank_fallback_total`
- `kb_feedback_avg_rating` → `pb_feedback_avg_rating`
- `kb_rate_limit_rejected_total` → `pb_rate_limit_rejected_total`

- [ ] **Step 2: Rename logger and server name in server.py**

- `logging.getLogger("kb-mcp")` → `logging.getLogger("pb-mcp")` (line 110)
- `trace.get_tracer("kb-mcp-server")` → `trace.get_tracer("pb-mcp-server")` (line 161)
- `Server("kb-mcp-server")` → `Server("pb-mcp-server")` (line 755)

- [ ] **Step 3: Rename OPA data paths in server.py**

Replace all `/v1/data/kb/` → `/v1/data/pb/` (lines 414, 421, 428, 488, 511, 643, 649, 1252).

- [ ] **Step 4: Rename Qdrant collection names in server.py**

Replace all collection name references:
- `knowledge_general` → `pb_general`
- `knowledge_code` → `pb_code`
- `knowledge_rules` → `pb_rules`

This includes enum values (line 770), defaults (lines 771, 1026, 1075, 1676), and hardcoded refs (line 1349, 1375).

- [ ] **Step 5: Rename DB connection string in server.py**

Replace `postgresql://kb_admin:changeme@localhost:5432/knowledgebase` → `postgresql://pb_admin:changeme@localhost:5432/powerbrain` (line 60).

- [ ] **Step 6: Rename logger in graph_service.py**

Replace `logging.getLogger("kb-graph")` → `logging.getLogger("pb-graph")` (line 35).

- [ ] **Step 7: Rename manage_keys.py**

- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 22)
- `KEY_PREFIX = "kb_"` → `KEY_PREFIX = "pb_"` (line 24)
- Comment: `kb_ prefix` → `pb_ prefix` (line 29)
- argparse description: `"KB MCP Server API Key Management"` → `"Powerbrain MCP Server API Key Management"` (line 115)

- [ ] **Step 8: Verify no remaining `kb` references**

Run: `grep -rn "kb" mcp-server/*.py mcp-server/**/*.py` — should return nothing (except maybe generic English words).

- [ ] **Step 9: Commit**

```bash
git add mcp-server/
git commit -m "refactor: rename kb → pb in mcp-server (metrics, OPA paths, collections)"
```

---

### Task 5: MCP Server Tests

**Files:**
- Modify: `mcp-server/tests/test_auth.py`
- Modify: `mcp-server/tests/test_opa_policy.py`
- Modify: `mcp-server/tests/test_layer_search.py`

- [ ] **Step 1: Rename token references in test_auth.py**

Replace all `kb_test_key`, `kb_invalid_key`, `kb_my_token` → `pb_test_key`, `pb_invalid_key`, `pb_my_token` (lines 25, 32, 44, 59, 70, 71).

- [ ] **Step 2: Rename OPA path assertions in test_opa_policy.py**

Replace `/v1/data/kb/access/allow` → `/v1/data/pb/access/allow` (line 72).

- [ ] **Step 3: Rename OPA path mocks in test_layer_search.py**

Replace all `/v1/data/kb/layers/max_layer` → `/v1/data/pb/layers/max_layer` (lines 132, 142, 152, 162, 172, 182).

- [ ] **Step 4: Run mcp-server tests**

Run: `cd mcp-server && python3 -m pytest tests/ -v`

- [ ] **Step 5: Commit**

```bash
git add mcp-server/tests/
git commit -m "refactor: rename kb → pb in mcp-server tests"
```

---

### Task 6: Ingestion Service — Collections, Logger, OPA Paths, DB Connection

**Files:**
- Modify: `ingestion/ingestion_api.py`
- Modify: `ingestion/snapshot_service.py`
- Modify: `ingestion/pii_scanner.py`
- Modify: `ingestion/retention_cleanup.py`
- Modify: `ingestion/backfill_layers.py`

- [ ] **Step 1: Rename ingestion_api.py**

- API title: `"KB Ingestion API"` → `"Powerbrain Ingestion API"` (line 73)
- Logger: `"kb-ingestion"` → `"pb-ingestion"` (line 70)
- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 37)
- `DEFAULT_COLLECTION = "knowledge_general"` → `DEFAULT_COLLECTION = "pb_general"` (line 67)
- Pydantic model default: `collection: str = "knowledge_general"` → `collection: str = "pb_general"` (line 147)
- OPA path: `/v1/data/kb/privacy` → `/v1/data/pb/privacy` (line 218)
- Comment OPA paths: `kb/privacy/pii_action`, `kb/privacy/dual_storage_enabled` → `pb/privacy/...` (line 208)

- [ ] **Step 2: Rename snapshot_service.py**

- Logger: `"kb-snapshot"` → `"pb-snapshot"` (line 27)
- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 31)
- Collections: `["knowledge_general", "knowledge_code", "knowledge_rules"]` → `["pb_general", "pb_code", "pb_rules"]` (line 35)
- Forgejo ref: `kb-org/kb-policies` → `pb-org/pb-policies` (line 116)

- [ ] **Step 3: Rename pii_scanner.py**

- Logger: `"kb-pii"` → `"pb-pii"` (line 34)

- [ ] **Step 4: Rename retention_cleanup.py**

- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 28)

- [ ] **Step 5: Rename backfill_layers.py**

- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 48)
- Collections: all `knowledge_general`/`knowledge_code`/`knowledge_rules` → `pb_general`/`pb_code`/`pb_rules` (lines 60, 495)
- CLI help text: update collection names in argparse help (line 495)
- Usage example in docstring (line 15): `knowledge_code` → `pb_code`

- [ ] **Step 6: Verify**

Run: `grep -rn "kb\|knowledge_general\|knowledge_code\|knowledge_rules" ingestion/*.py` — should return nothing.

- [ ] **Step 7: Commit**

```bash
git add ingestion/
git commit -m "refactor: rename kb → pb in ingestion service"
```

---

### Task 7: Ingestion Tests

**Files:**
- Modify: `ingestion/tests/test_layer_generation.py`

- [ ] **Step 1: Rename collection references in test_layer_generation.py**

Replace all `knowledge_general` → `pb_general` and `knowledge_code` → `pb_code` (lines 184, 215, 252, 278, 314, 338, 375, 405, 443).

- [ ] **Step 2: Run ingestion tests**

Run: `cd ingestion && python3 -m pytest tests/ -v`

- [ ] **Step 3: Commit**

```bash
git add ingestion/tests/
git commit -m "refactor: rename kb → pb in ingestion tests"
```

---

### Task 8: Reranker Service — Metrics, API Title

**Files:**
- Modify: `reranker/service.py`

- [ ] **Step 1: Rename Prometheus metrics**

Replace (lines 36-51):
- `kb_reranker_requests_total` → `pb_reranker_requests_total`
- `kb_reranker_duration_seconds` → `pb_reranker_duration_seconds`
- `kb_reranker_batch_size` → `pb_reranker_batch_size`
- `kb_reranker_model_load_seconds` → `pb_reranker_model_load_seconds`

- [ ] **Step 2: Rename API title**

Replace `"KB Reranker Service"` → `"Powerbrain Reranker Service"` (line 75).

- [ ] **Step 3: Verify**

Run: `grep -n "kb" reranker/service.py` — should return nothing.

- [ ] **Step 4: Commit**

```bash
git add reranker/
git commit -m "refactor: rename kb → pb in reranker service"
```

---

### Task 9: pb-proxy — Auth, OPA Path, Token References

**Files:**
- Modify: `pb-proxy/auth.py`
- Modify: `pb-proxy/proxy.py`
- Modify: `pb-proxy/tool_injection.py`
- Modify: `pb-proxy/config.py`

- [ ] **Step 1: Rename auth.py**

- Comment: `kb_ API keys` → `pb_ API keys` (line 3)
- Comment: `non-kb_ prefixed` → `non-pb_ prefixed` (line 54)
- Token validation: `token.startswith("kb_")` → `token.startswith("pb_")` (line 58)

- [ ] **Step 2: Rename proxy.py OPA path**

Replace `/v1/data/kb/proxy` → `/v1/data/pb/proxy` (line 112).

- [ ] **Step 3: Rename tool_injection.py comments**

Replace `kb_ API key` → `pb_ API key` (lines 35, 220).

- [ ] **Step 4: Rename config.py DB default**

Replace default DB name if it contains `knowledgebase` or verify `PG_DATABASE` default is already `powerbrain` (line 66 — check current value).

- [ ] **Step 5: Verify**

Run: `grep -rn "kb" pb-proxy/*.py` — should return nothing.

- [ ] **Step 6: Commit**

```bash
git add pb-proxy/auth.py pb-proxy/proxy.py pb-proxy/tool_injection.py pb-proxy/config.py
git commit -m "refactor: rename kb → pb in pb-proxy (auth, OPA paths)"
```

---

### Task 10: pb-proxy Tests

**Files:**
- Modify: `pb-proxy/tests/test_auth.py`
- Modify: `pb-proxy/tests/test_proxy_auth.py`
- Modify: `pb-proxy/tests/test_tool_injection.py`
- Modify: `pb-proxy/tests/test_agent_loop.py`

- [ ] **Step 1: Rename all `kb_` token references in test files**

Replace all occurrences:
- `test_auth.py`: `kb_test_valid_key_*` → `pb_test_valid_key_*`, `kb_invalid_key_*` → `pb_invalid_key_*`, `kb_cached_key_*` → `pb_cached_key_*`, test name `test_verify_non_kb_prefix` → `test_verify_non_pb_prefix`, comment `Non-kb_ prefixed` → `Non-pb_ prefixed`
- `test_proxy_auth.py`: `kb_invalid_key_*` → `pb_invalid_key_*`, `kb_valid_key_*` → `pb_valid_key_*`
- `test_tool_injection.py`: `kb_user_key_*` → `pb_user_key_*`
- `test_agent_loop.py`: `kb_test_token_*` → `pb_test_token_*`

- [ ] **Step 2: Run pb-proxy tests**

Run: `cd pb-proxy && python3 -m pytest tests/ -v`

- [ ] **Step 3: Commit**

```bash
git add pb-proxy/tests/
git commit -m "refactor: rename kb → pb in pb-proxy tests"
```

---

### Task 11: Evaluation Module

**Files:**
- Modify: `evaluation/run_eval.py`
- Modify: `evaluation/tests/test_search_with_opa.py`

- [ ] **Step 1: Rename run_eval.py**

- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 32)
- OPA path: `/v1/data/kb/access/allow` → `/v1/data/pb/access/allow` (line 136)
- Collection default: `knowledge_general` → `pb_general` (line 227)

- [ ] **Step 2: Rename test_search_with_opa.py**

- OPA path assertion: `/v1/data/kb/access/allow` → `/v1/data/pb/access/allow` (line 95)

- [ ] **Step 3: Commit**

```bash
git add evaluation/
git commit -m "refactor: rename kb → pb in evaluation module"
```

---

### Task 12: Monitoring — Grafana Dashboards, Alerting Rules, Datasource

**Files:**
- Rename + modify: `monitoring/grafana-dashboards/kb-overview.json` → `pb-overview.json`
- Rename + modify: `monitoring/grafana-dashboards/kb-queries.json` → `pb-queries.json`
- Rename + modify: `monitoring/grafana-dashboards/kb-search-quality.json` → `pb-search-quality.json`
- Rename + modify: `monitoring/grafana-dashboards/kb-infrastructure.json` → `pb-infrastructure.json`
- Modify: `monitoring/grafana-dashboards/dashboards.yml`
- Modify: `monitoring/alerting_rules.yml`
- Modify: `monitoring/grafana-datasources/datasources.yml`

- [ ] **Step 1: Rename dashboard files**

```bash
git mv monitoring/grafana-dashboards/kb-overview.json monitoring/grafana-dashboards/pb-overview.json
git mv monitoring/grafana-dashboards/kb-queries.json monitoring/grafana-dashboards/pb-queries.json
git mv monitoring/grafana-dashboards/kb-search-quality.json monitoring/grafana-dashboards/pb-search-quality.json
git mv monitoring/grafana-dashboards/kb-infrastructure.json monitoring/grafana-dashboards/pb-infrastructure.json
```

- [ ] **Step 2: Update metric names inside dashboard JSON files**

In all 4 dashboard files, replace all `kb_` metric prefixes → `pb_`:
- `kb_mcp_requests_total` → `pb_mcp_requests_total`
- `kb_mcp_request_duration_seconds_bucket` → `pb_mcp_request_duration_seconds_bucket`
- `kb_feedback_avg_rating` → `pb_feedback_avg_rating`
- `kb_mcp_rerank_fallback_total` → `pb_mcp_rerank_fallback_total`
- `kb_mcp_policy_decisions_total` → `pb_mcp_policy_decisions_total`
- `kb_mcp_search_results_count_bucket` → `pb_mcp_search_results_count_bucket`
- `kb_reranker_duration_seconds_bucket` → `pb_reranker_duration_seconds_bucket`
- `kb_reranker_model_load_seconds_sum` → `pb_reranker_model_load_seconds_sum`
- `kb_reranker_model_load_seconds_count` → `pb_reranker_model_load_seconds_count`
- `kb_reranker_batch_size_bucket` → `pb_reranker_batch_size_bucket`

Also update UIDs:
- `"uid": "kb-overview"` → `"uid": "pb-overview"`
- `"uid": "kb-queries"` → `"uid": "pb-queries"`
- `"uid": "kb-search-quality"` → `"uid": "pb-search-quality"`
- `"uid": "kb-infrastructure"` → `"uid": "pb-infrastructure"`

- [ ] **Step 3: Update dashboards.yml**

Replace `kb-dashboards` → `pb-dashboards` (line 4).

- [ ] **Step 4: Update alerting_rules.yml**

Replace all `kb_` metric names → `pb_` and group name `kb-alerts` → `pb-alerts` (lines 2, 8, 19, 29, 47, 57, 86).

- [ ] **Step 5: Update datasources.yml**

- `user: kb_admin` → `user: pb_admin` (line 28)
- `database: knowledgebase` → `database: powerbrain` (line 31)

- [ ] **Step 6: Verify**

Run: `grep -rn "kb" monitoring/` — should return nothing.

- [ ] **Step 7: Commit**

```bash
git add monitoring/
git commit -m "refactor: rename kb → pb in monitoring (dashboards, alerts, datasource)"
```

---

### Task 13: Test Data — Seed Files, Collection Names

**Files:**
- Modify: `testdata/documents.json`
- Modify: `testdata/seed.py`

- [ ] **Step 1: Rename collection names in documents.json**

Replace all occurrences:
- `"collection": "knowledge_general"` → `"collection": "pb_general"`
- `"collection": "knowledge_code"` → `"collection": "pb_code"`
- `"collection": "knowledge_rules"` → `"collection": "pb_rules"`

- [ ] **Step 2: Rename seed.py references**

Replace collection name strings:
- `"knowledge_general"` → `"pb_general"` (lines 219, etc.)
- `"knowledge_code"` → `"pb_code"` (line 220)
- `"knowledge_rules"` → `"pb_rules"` (line 221)

- [ ] **Step 3: Commit**

```bash
git add testdata/
git commit -m "refactor: rename kb → pb in test data"
```

---

### Task 14: Scripts

**Files:**
- Modify: `scripts/smoke_search_first_mvp.py`
- Modify: `scripts/seed_demo_search_data.py`

- [ ] **Step 1: Rename collection references in scripts**

Replace all `knowledge_general`, `knowledge_code`, `knowledge_rules` → `pb_general`, `pb_code`, `pb_rules`.

- [ ] **Step 2: Commit**

```bash
git add scripts/
git commit -m "refactor: rename kb → pb in scripts"
```

---

### Task 15: Integration Tests

**Files:**
- Modify: `tests/integration/e2e/conftest.py`
- Modify: `tests/integration/test_auth.py`

- [ ] **Step 1: Rename conftest.py**

- Token prefix: `"kb_test_"` → `"pb_test_"` (line 222)
- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 37)
- Collections: `["knowledge_general", "knowledge_code", "knowledge_rules"]` → `["pb_general", "pb_code", "pb_rules"]` (line 50)

- [ ] **Step 2: Rename test_auth.py**

- Connection string: `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain` (line 18)

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "refactor: rename kb → pb in integration tests"
```

---

### Task 16: Documentation — CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update all `kb-` container references**

Replace all `kb-` container name references → `pb-` (e.g., `kb-qdrant` → `pb-qdrant`, `docker exec kb-ollama` → `docker exec pb-ollama`, `docker exec kb-opa` → `docker exec pb-opa`).

- [ ] **Step 2: Update API token prefix references**

Replace all `kb_` token references → `pb_` (e.g., `kb_ API key` → `pb_ API key`, `Bearer kb_<key>` → `Bearer pb_<key>`).

- [ ] **Step 3: Update OPA policy references**

Replace all `kb.access`, `kb.proxy`, `kb.privacy`, `kb.summarization`, `kb.layers`, `data.kb.*` → `pb.*`.

- [ ] **Step 4: Update directory path**

Replace `opa-policies/kb/` → `opa-policies/pb/` and `/policies/kb/` → `/policies/pb/`.

- [ ] **Step 5: Update DB references**

Replace `kb_admin` → `pb_admin`, `knowledgebase` → `powerbrain`.

- [ ] **Step 6: Update Qdrant collection names**

Replace `knowledge_general`, `knowledge_code`, `knowledge_rules` → `pb_general`, `pb_code`, `pb_rules`.

- [ ] **Step 7: Update metric name references**

Replace `kb_mcp_*`, `kb_feedback_*`, `kb_rate_limit_*`, `kb_reranker_*` → `pb_*`.

- [ ] **Step 8: Update Forgejo references**

Replace `kb-policies`, `kb-schemas`, `kb-docs`, `kb-org` → `pb-policies`, `pb-schemas`, `pb-docs`, `pb-org`.

- [ ] **Step 9: Update Completed Features list**

Replace any `kb.` references in feature descriptions → `pb.`.

- [ ] **Step 10: Verify**

Run: `grep -n "kb" CLAUDE.md` — should return nothing except possibly English words. Check manually.

- [ ] **Step 11: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for kb → pb rename"
```

---

### Task 17: Documentation — README.md and docs/

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/deployment.md`
- Modify: `docs/scalability.md`
- Modify: `docs/technology-decisions.md`

- [ ] **Step 1: Update README.md**

Replace Qdrant collection creation commands: `knowledge_general knowledge_code knowledge_rules` → `pb_general pb_code pb_rules` and any other `kb` references.

- [ ] **Step 2: Update docs/architecture.md**

Replace collection names (`knowledge_general`, `knowledge_code`, `knowledge_rules` → `pb_general`, `pb_code`, `pb_rules`) and any `kb_` metric references.

- [ ] **Step 3: Update docs/deployment.md**

Replace collection names in setup commands and any `kb` references.

- [ ] **Step 4: Update docs/scalability.md**

Replace `knowledge_general` → `pb_general` in collection examples.

- [ ] **Step 5: Update docs/technology-decisions.md**

Replace `kb-org` → `pb-org` and any other `kb` references.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/
git commit -m "docs: update documentation for kb → pb rename"
```

---

### Task 18: Skills — Querying Knowledge Base

**Files:**
- Modify: `skills/querying-knowledge-base/SKILL.md`

- [ ] **Step 1: Update collection references**

Replace all `knowledge_general`, `knowledge_code`, `knowledge_rules` → `pb_general`, `pb_code`, `pb_rules` throughout the skill file.

- [ ] **Step 2: Commit**

```bash
git add skills/
git commit -m "docs: update skills for kb → pb rename"
```

---

### Task 19: Final Verification

- [ ] **Step 1: Global search for remaining `kb_` references**

Run: `grep -rn "kb_" --include="*.py" --include="*.yml" --include="*.yaml" --include="*.json" --include="*.sql" --include="*.rego" --include="*.md" . | grep -v "docs/plans/" | grep -v "docs/superpowers/plans/" | grep -v ".git/"`

- [ ] **Step 2: Global search for remaining `kb-` references**

Run: `grep -rn '"kb-\|kb-[a-z]' --include="*.py" --include="*.yml" --include="*.yaml" --include="*.json" --include="*.sql" --include="*.rego" --include="*.md" . | grep -v "docs/plans/" | grep -v "docs/superpowers/plans/" | grep -v ".git/"`

- [ ] **Step 3: Check for `knowledge_general/code/rules` leftovers**

Run: `grep -rn "knowledge_general\|knowledge_code\|knowledge_rules" --include="*.py" --include="*.yml" --include="*.yaml" --include="*.json" --include="*.sql" --include="*.rego" --include="*.md" . | grep -v "docs/plans/" | grep -v "docs/superpowers/plans/"`

- [ ] **Step 4: Check for `knowledgebase` DB name leftovers**

Run: `grep -rn "knowledgebase" --include="*.py" --include="*.yml" --include="*.yaml" --include="*.json" --include="*.sql" --include="*.md" . | grep -v "docs/plans/"`

- [ ] **Step 5: Check for `package kb\.` leftovers**

Run: `grep -rn "package kb\." --include="*.rego" .`

- [ ] **Step 6: Run unit tests**

```bash
cd mcp-server && python3 -m pytest tests/ -v
cd ../pb-proxy && python3 -m pytest tests/ -v
cd ../ingestion && python3 -m pytest tests/ -v
cd ../reranker && python3 -m pytest tests/ -v 2>/dev/null || true
```

- [ ] **Step 7: Final commit for any stragglers**

If any remaining references were found and fixed:
```bash
git add -A
git commit -m "refactor: fix remaining kb → pb references"
```

---

### Post-Migration Notes

After all code changes are committed, to deploy:

```bash
# Stop and remove all containers + volumes
docker compose down -v

# Recreate everything fresh
docker compose up -d

# Pull embedding model
docker exec pb-ollama ollama pull nomic-embed-text

# Create new Qdrant collections
for col in pb_general pb_code pb_rules; do
  curl -s -X PUT "http://localhost:6333/collections/$col" \
    -H 'Content-Type: application/json' \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}'
done

# Verify OPA policies loaded
docker exec pb-opa /opa test /policies/pb/ -v

# Seed test data
docker compose run --rm seed
```

**External actions (manual, outside this codebase):**
- Rename Forgejo organization: `kb-org` → `pb-org`
- Rename Forgejo repositories: `kb-policies` → `pb-policies`, `kb-schemas` → `pb-schemas`, `kb-docs` → `pb-docs`
- Update any external systems using `kb_` API keys to use `pb_` prefix
- Update any external monitoring dashboards or alerts referencing `kb_*` metrics
