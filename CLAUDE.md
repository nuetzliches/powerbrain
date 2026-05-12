# CLAUDE.md — Powerbrain Context Engine

## Project Overview

Open-source context engine that feeds AI agents with policy-compliant enterprise knowledge.
Agents access data exclusively through the Model Context Protocol (MCP).
All components are open source and run as Docker containers. Self-hosted, GDPR-native.

## Architecture

```
Agent/Skill
    │ MCP
    ▼
┌─────────────────────────────────────────────────┐
│  MCP Server (FastAPI, 28 tools)                  │
│  ├─ OPA Policy Check (every request)            │
│  ├─ Qdrant Vector Search (oversampled)          │
│  ├─ Reranker (Cross-Encoder, Top-N)             │
│  ├─ Context Summarization (OPA-controlled)      │
│  ├─ Sealed Vault (PII pseudonymization)         │
│  ├─ PostgreSQL (structured data)                │
│  └─ Audit Log (GDPR-compliant)                  │
└─────────────────────────────────────────────────┘
    │           │           │           │
    ▼           ▼           ▼           ▼
 Qdrant    PostgreSQL     OPA       Reranker
 (vectors)  (data+vault+graph) (policies) (Cross-Enc.)
    │
    ▼
 Ollama / vLLM / TEI
 (embeddings + summarization, configurable)
```

## Directory Structure

```
powerbrain/
├── CLAUDE.md              ← You are here
├── README.md              ← Quick start and overview
├── docker-compose.yml     ← All services
├── .env.example           ← Environment variables
├── shared/
│   ├── __init__.py
│   ├── config.py           ← read_secret(), build_postgres_url(), pool sizes
│   ├── llm_provider.py     ← OpenAI-compat LLM provider abstraction
│   ├── telemetry.py        ← OTel init, trace_operation, MetricsAggregator
│   ├── rerank_provider.py  ← Configurable reranker backend (Powerbrain/TEI/Cohere)
│   ├── drift_check.py      ← Embedding drift detection (Art. 15)
│   ├── embedding_cache.py  ← In-process TTL cache for embeddings
│   └── tests/
│       ├── test_llm_provider.py
│       ├── test_rerank_provider.py
│       ├── test_telemetry.py
│       ├── test_embedding_cache.py
│       └── test_drift_check.py
├── mcp-server/
│   ├── server.py          ← MCP Server (28 tools)
│   ├── graph_service.py   ← Knowledge Graph (Apache AGE)
│   ├── compliance_doc.py  ← EU AI Act Annex IV generator
│   ├── policy_admin_page.py ← (reserved for future UI)
│   ├── Dockerfile
│   └── requirements.txt
├── reranker/
│   ├── service.py         ← Cross-Encoder service
│   ├── Dockerfile
│   └── requirements.txt
├── ingestion/
│   ├── pii_scanner.py     ← PII detection (Presidio)
│   ├── pii_config.yaml    ← PII scanner config (entity types, custom recognizers)
│   ├── retention_cleanup.py ← GDPR retention cleanup jobs
│   ├── sync_service.py    ← Repository sync orchestration (incremental)
│   ├── repos.yaml.example      ← Repository sync configuration template
│   ├── office365.yaml.example  ← Office 365 sync configuration template
│   ├── adapters/
│   │   ├── base.py        ← NormalizedDocument, SourceAdapter ABC
│   │   ├── git_adapter.py ← Git adapter (include/exclude, skip patterns)
│   │   ├── providers/
│   │   │   └── github.py  ← GitHub REST API (PAT + GitHub App auth)
│   │   └── office365/     ← Office 365 adapter (separate package)
│   │       ├── adapter.py       ← Office365Adapter(SourceAdapter)
│   │       ├── graph_client.py  ← Auth, $batch, RU-tracking, retry
│   │       ├── content.py       ← markitdown + fallback extraction
│   │       ├── requirements.txt ← msal, markitdown, python-docx, etc.
│   │       ├── providers/
│   │       │   ├── sharepoint.py ← SharePoint/OneDrive (Delta Query)
│   │       │   ├── outlook.py    ← Outlook Mail (Delta Query)
│   │       │   ├── teams.py      ← Teams Messages (Delta Query + dedup)
│   │       │   └── onenote.py    ← OneNote (Delegated Auth, no delta)
│   │       └── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── init-db/
│   ├── 001_schema.sql     ← Base schema
│   ├── 002_privacy.sql    ← Privacy extensions
│   ├── 003_knowledge_graph.sql ← Apache AGE graph setup
│   ├── 007_pii_vault.sql  ← Sealed Vault (PII originals + mappings)
│   ├── 014_audit_hashchain.sql ← Tamper-resistant audit log (Art. 12)
│   ├── 015_human_oversight.sql ← Circuit breaker + approval queue (Art. 14)
│   ├── 016_data_quality.sql    ← Quality scoring (Art. 10)
│   ├── 017_accuracy_monitoring.sql ← Drift detection (Art. 15)
│   ├── 018_repo_sync_state.sql    ← Repository sync state tracking
│   └── 019_sync_state_delta.sql   ← Delta link support for Office 365
├── opa-policies/pb/
│   ├── data.json           ← Policy data (configurable without Rego knowledge)
│   ├── policy_data_schema.json ← JSON Schema for data.json validation
│   ├── access.rego         ← Access control (logic only, data from data.json)
│   ├── rules.rego          ← Business rules (logic only)
│   ├── privacy.rego        ← GDPR policies (logic only)
│   ├── summarization.rego  ← Context summarization policies (logic only)
│   ├── proxy.rego          ← Proxy policies (logic only)
│   ├── oversight.rego      ← Human oversight policies (Art. 14)
│   └── ingestion.rego      ← Data quality gate policies (Art. 10)
├── caddy/
│   └── Caddyfile           ← Reverse proxy config (optional TLS profile)
├── secrets/
│   └── .gitkeep            ← Docker Secrets directory (*.txt files gitignored)
├── monitoring/
│   ├── prometheus.yml      ← Prometheus config
│   ├── alerting_rules.yml  ← Alert rules
│   ├── tempo.yml           ← Distributed tracing config
│   ├── grafana-dashboards/ ← Provisioned dashboards
│   └── grafana-datasources/← Provisioned data sources
├── pb-proxy/
│   ├── proxy.py           ← Main FastAPI application
│   ├── auth.py            ← API-key auth (ProxyKeyVerifier, asyncpg)
│   ├── middleware.py      ← ASGI auth middleware (global pb_ key validation)
│   ├── tool_injection.py  ← Multi-server MCP tool discovery + merge
│   ├── agent_loop.py      ← Tool-call execution loop with server routing
│   ├── mcp_config.py      ← MCP server config model + YAML loader
│   ├── config.py          ← Configuration
│   ├── litellm_config.yaml← LLM provider config (+ provider_keys section)
│   ├── mcp_servers.yaml   ← MCP server connections (name, URL, auth)
│   ├── Dockerfile
│   └── requirements.txt
├── worker/
│   ├── scheduler.py       ← APScheduler setup + job registration
│   ├── jobs/
│   │   ├── accuracy_metrics.py  ← Art. 15 drift + feedback refresh
│   │   ├── audit_retention.py   ← Art. 12 checkpoint + prune
│   │   ├── gdpr_retention.py    ← GDPR retention cleanup
│   │   ├── pending_review_timeout.py ← Art. 14 review expiry
│   │   └── repo_sync.py        ← GitHub/Git repository sync trigger
│   ├── Dockerfile
│   └── requirements.txt
├── scripts/
│   ├── quickstart.sh          ← Automated first-time setup (--seed / --demo flags)
│   ├── build-images.sh        ← Docker image build script
│   ├── seed_graph.py          ← Knowledge-graph seed (used by pb-seed in demo mode)
│   └── seed_*.py              ← Test data seeding scripts
├── demo/
│   ├── app.py                 ← Streamlit entry (pb-demo container)
│   ├── mcp_client.py          ← MCP HTTP wrapper + vault-token builder
│   ├── panels/
│   │   ├── search_roles.py    ← Tab A — OPA role contrast
│   │   ├── pii_vault.py       ← Tab B — scan/ingest/reveal vault flow
│   │   └── knowledge_graph.py ← Tab C — NovaTech org-chart (streamlit-agraph)
│   ├── assets/talk_track.md   ← Presenter cheat-sheet (rendered in sidebar)
│   ├── Dockerfile
│   └── requirements.txt
├── tests/
│   ├── integration/           ← E2E smoke tests (gated behind RUN_INTEGRATION_TESTS=1)
│   └── load/
│       ├── locustfile.py      ← Locust load test for MCP search pipeline
│       └── README.md          ← Load test instructions
├── SECURITY.md                ← Vulnerability reporting policy
└── docs/
    ├── getting-started.md          ← Step-by-step tutorial for newcomers
    ├── playbook-sales-demo.md      ← 15-min decision-maker demo script (Tabs A/B/C)
    ├── mcp-tools.md                ← All 23 MCP tools with parameters and access roles
    ├── what-is-powerbrain.md       ← Detailed overview and positioning
    ├── deployment.md               ← Dev, prod, TLS, Docker Secrets guide
    ├── architecture.md             ← Technical deep-dive (components, GDPR)
    ├── KNOWN_ISSUES.md             ← Resolved issues archive (P0–P3)
    ├── technology-decisions.md     ← ADRs (VLM, vLLM, Git adapter, OTel)
    ├── scalability.md              ← Scaling, load balancing, caching
    └── gdpr-external-ai-services.md ← Legal assessment for external LLMs
```

## Components and Ports

| Service       | Port  | Technology                         | Purpose                          |
|---------------|-------|------------------------------------|----------------------------------|
| mcp-server    | 8080  | Python, FastAPI, MCP SDK           | Single agent access point        |
| reranker      | 8082  | Python, sentence-transformers      | Cross-Encoder reranking          |
| ingestion     | 8081  | Python, FastAPI                    | ETL, chunking, embedding         |
| qdrant        | 6333  | Qdrant                             | Vector database                  |
| postgres      | 5432  | PostgreSQL 16 + Apache AGE         | Structured data + graph + audit  |
| opa           | 8181  | Open Policy Agent                  | Policy engine + access control   |
| ollama        | 11434 | Ollama (optional, `local-llm`)     | Local embeddings + summarization |
| vllm          | 8000  | vLLM (optional, `gpu` profile)     | Production LLM serving           |
| tei           | 8010  | HF TEI (optional, `gpu` profile)   | Production embedding serving     |
| caddy         | 80/443| Caddy 2 (optional, `tls` profile)  | TLS reverse proxy                |
| git server    | —     | Any Git server (external, optional)| Git repos, policies, schemas     |
| prometheus    | 9090  | Prometheus                         | Metrics collection               |
| grafana       | 3001  | Grafana                            | Dashboards + visualization       |
| tempo         | 4317  | Grafana Tempo                      | Distributed tracing              |
| pb-proxy      | 8090  | Python, FastAPI, LiteLLM, MCP SDK    | AI Provider Proxy (optional)     |
| pb-worker     | —     | Python, APScheduler                  | Maintenance jobs (internal only) |
| opal-server   | 7002  | OPAL (optional, `opal` profile)      | Policy sync from git repo        |
| opal-client   | —     | OPAL (optional, `opal` profile)      | Pushes updates to OPA            |

## Key Concepts

### Data Classification
Every data object has a classification level:
- `public` — Accessible to all agents
- `internal` — Only for roles analyst, admin, developer
- `confidential` — Only admin
- `restricted` — Admin + explicit purpose

OPA checks classification on **every** MCP request.

### Search Pipeline (3-stage)
1. **Qdrant** returns `top_k × 5` results (oversampling)
2. **OPA** filters by policy and classification
3. **Reranker** scores query-document relevance, returns top-k

Reranker backend is configurable via `RERANKER_BACKEND`:
- `powerbrain` (default) — built-in Cross-Encoder service
- `tei` — HuggingFace Text Embeddings Inference `/rerank` endpoint
- `cohere` — Cohere Rerank API v2 (external, requires API key)

Abstraction: `shared/rerank_provider.py` (follows `shared/llm_provider.py` pattern).
If the reranker is down → graceful fallback to Qdrant ordering.

### Context Summarization (OPA-controlled)
After search and reranking, summarization is policy-controlled:
- `pb.summarization.summarize_allowed` — all roles except viewer may request summaries
- `pb.summarization.summarize_required` — confidential data: only summaries, never raw chunks
- `pb.summarization.summarize_detail` — restricted data gets `brief` summaries only

Agents use `summarize=true` and `summary_detail` parameters on `search_knowledge` and `get_code_context`.
Response includes `summary` (text) and `summary_policy` (`requested` | `enforced` | `denied`).
Graceful degradation: if LLM summarization fails → raw chunks returned.

Config: `LLM_MODEL` (default: `qwen2.5:3b`), `SUMMARIZATION_ENABLED` (default: `true`).

### Sealed Vault (Dual Storage)
PII data is stored in two tiers:
1. **Qdrant** contains only pseudonymized text (deterministic, per-project salt)
2. **pii_vault schema** (PostgreSQL, RLS) stores originals + mapping

Access to originals requires:
- HMAC-signed token with expiration
- OPA policy check (`vault_access_allowed`)
- Purpose binding (only allowed purposes)
- Fields redacted by purpose (`vault_fields_to_redact`)

Art. 17 deletion: delete vault mapping → pseudonyms become irreversible.

### MCP Tools (28)
- `search_knowledge` — Semantic search (Qdrant + reranking); supports `summarize`, `summary_detail`, `rerank_options` (incl. `boost_corrections`); optional PII originals via vault token; metadata PII redaction
- `query_data` — Structured queries (PostgreSQL)
- `get_rules` — Business rules for a context
- `check_policy` — OPA policy evaluation
- `ingest_data` — Ingest new data
- `get_classification` — Classification lookup
- `list_datasets` — List available datasets
- `get_code_context` — Code search (Qdrant + reranking); supports `summarize` and `summary_detail`; metadata PII redaction
- `graph_query` — Knowledge graph queries (nodes, relationships, paths); PII-masked output
- `graph_mutate` — Knowledge graph mutations (developer/admin only); PII-masked output
- `get_document` — Retrieve document by ID at specific context layer (L0/L1/L2) for progressive loading
- `delete_documents` — Bulk-delete documents by filter (source_type, project, or all); deletes from Qdrant, PostgreSQL, Vault (cascade), and Knowledge Graph
- `submit_feedback` — Rate search result quality (1–5 stars)
- `get_eval_stats` — Retrieval quality statistics with windowed metrics
- `create_snapshot` — Knowledge versioning snapshot (admin only)
- `list_snapshots` — List available snapshots
- `manage_policies` — Read/update OPA policy data sections at runtime (admin only, JSON Schema validated)
- `generate_compliance_doc` — EU AI Act Annex IV technical documentation (admin only)
- `verify_audit_integrity` — Verify tamper-evident audit hash chain (admin only)
- `export_audit_log` — Export audit log entries as JSON/CSV (admin only)
- `get_system_info` — Transparency report (Art. 13) for deployers
- `review_pending` — List/approve/deny pending human oversight reviews (admin only)
- `get_review_status` — Poll status of a pending review
- `report_breach` — Report a potential privacy incident (GDPR Art. 33). Any authenticated role; creates `privacy_incidents` row, status=`detected`
- `list_incidents` — List incidents with filters (admin only); `attention=true` returns rows from `v_incidents_requiring_attention` (72-hour deadline warnings)
- `assess_incident` — Compute `notifiable_risk` via OPA risk-score (admin only); supports `force_notifiable` / `force_not_notifiable` admin overrides
- `notify_authority` — Record Art. 33 supervisory-authority notification (admin only); does not send, only documents
- `notify_data_subject` — Record Art. 34 data-subject notification (admin only); first call moves status, later calls append to ledger

### Privacy (GDPR)
- **PII Scanner** (Microsoft Presidio) at ingestion — configurable via `ingestion/pii_config.yaml` (entity types, custom recognizers, confidence, languages)
- **Purpose binding** via OPA policy (`pb.privacy`)
- **Retention periods** with automatic cleanup
- **Right to erasure** (Art. 17) with tracking table
- **Audit log** for every PII data access
- **Sealed Vault** for reversible pseudonymization (original in vault, pseudonym in Qdrant)
- **HMAC tokens** for time-limited vault access
- **2-tier deletion** (Art. 17): delete vault = pseudonyms become irreversible

### Docker Secrets
Sensitive values can be provided as Docker Secrets files in `./secrets/*.txt`:
- `pg_password.txt` — PostgreSQL password
- `vault_hmac_secret.txt` — Vault HMAC signing key
- `forgejo_token.txt` — Git server API token (any: Forgejo, GitHub, GitLab, etc.)
- `github_pat.txt` — GitHub PAT (AI Proxy provider key)
- `anthropic_api_key.txt` — Anthropic API key (AI Proxy provider key)
- `mcp_auth_token.txt` — Token for pb-proxy → mcp-server auth

Services read from `/run/secrets/<name>` with env var fallback for backward compatibility.
The `_read_secret()` helper checks `<ENV_VAR>_FILE` first, then falls back to `<ENV_VAR>`.

### Git Server Integration
No separate git container — uses any existing Git server (Forgejo, GitHub, GitLab, Gitea, etc.):
- `pb-policies` repo → OPA bundle polling (via OPAL or direct)
- `pb-schemas` repo → JSON schema validation
- `pb-docs` + project repos → Ingestion pipeline

Configured via `FORGEJO_URL` / `OPAL_POLICY_REPO_URL`. The env var names use "Forgejo" for historical reasons but accept any Git server URL.

### GitHub Adapter (Repository Sync)
Syncs GitHub repository contents into the knowledge base as a data source.

**Configuration:** `ingestion/repos.yaml` (see `repos.yaml.example`). Each entry: name, URL, branch, collection, project, classification, auth mode, include/exclude patterns.

**Sync modes:**
- **Polling** — pb-worker job every N minutes (configurable via `REPO_SYNC_INTERVAL_MINUTES`, default 5)
- **Manual** — `POST /sync/{repo_name}` on ingestion service (port 8081)
- **External webhook** — Tools like [Hookaido](https://github.com/nuetzliches/hookaido) can call the sync endpoint on push events

**Auth:** PAT (via `secrets/github_pat.txt`) or GitHub App (JWT + installation token, requires `app_id`, `installation_id`, `private_key_path` in repos.yaml).

**Incremental sync:** Tracks last commit SHA in `repo_sync_state` table. First sync fetches full tree, subsequent syncs use compare API (only changed files). Modified files: delete old → re-ingest new. Removed files: cascade-delete (Qdrant, PG, vault, graph).

**Pipeline:** All content flows through standard ingestion: chunking → PII scan → OPA policy → quality gate (`github` source_type, threshold 0.3) → embedding → context layers (L0/L1/L2).

**Default skip patterns:** Binary files, `.git/`, `node_modules/`, `vendor/`, `__pycache__/`, lock files. Additional filtering via include/exclude globs in config.

### LLM Provider Abstraction
Embedding and Summarization use the OpenAI-compatible API (`/v1/embeddings`, `/v1/chat/completions`).
Each can be pointed to a different backend via environment variables:
- `EMBEDDING_PROVIDER_URL` + `EMBEDDING_MODEL` — for vector embeddings
- `LLM_PROVIDER_URL` + `LLM_MODEL` — for summarization/generation
- Optional API keys: `EMBEDDING_API_KEY`, `LLM_API_KEY`

Falls back to `OLLAMA_URL` if provider URLs not set. Supports Ollama, vLLM, HF TEI, infinity, OpenAI.
Implementation: `shared/llm_provider.py` — `EmbeddingProvider` and `CompletionProvider` classes.
Optional GPU stack: `docker compose --profile gpu up -d` (vLLM + HF TEI).

### AI Provider Proxy (optional)
Optional gateway activated via `docker compose --profile proxy up`.
Sits between AI consumers and LLM providers:
1. Client authenticates with `pb_` API key (same keys as MCP server, stored in `api_keys` table)
2. Proxy injects Powerbrain MCP tools into `tools[]` array (from N configured MCP servers)
3. Forwards augmented request to LLM (via LiteLLM, 100+ providers)
4. When LLM returns tool calls → proxy routes to correct MCP server via prefix-based namespacing
5. Repeats until final response, then returns to client

**Authentication:**
- `AUTH_REQUIRED=true` (default) — every request needs `Authorization: Bearer pb_<key>`
- Pure ASGI middleware (`ProxyAuthMiddleware` in `pb-proxy/middleware.py`) validates `pb_` tokens globally on all endpoints
- `ProxyKeyVerifier` (`pb-proxy/auth.py`) validates against PostgreSQL `api_keys` table
- Middleware populates ASGI `scope["state"]` with `agent_id` and `agent_role` for downstream use
- Identity propagation: user's `pb_` key is forwarded to MCP servers (each tool call authenticated as the user)
- Exempt paths: `/health` and `/metrics/json` bypass auth

**Per-Provider LLM Key Management:**
- `X-Provider-Key` header allows clients to supply their own LLM provider API key
- Per-provider `key_source` configured in `litellm_config.yaml` under `provider_keys` section:
  - **central** (default): Uses env var / Docker Secret from `PROVIDER_KEY_MAP`; ignores `X-Provider-Key`
  - **user**: Requires `X-Provider-Key` header from client; returns 401 if missing
  - **hybrid**: Prefers `X-Provider-Key` header, falls back to env var / Docker Secret
- Provider extracted from model string (e.g., `"anthropic/claude-3"` → `"anthropic"`)
- Unconfigured providers default to `central` mode (backward compatible)

**Multi-MCP-Server Aggregation:**
- Configured via `pb-proxy/mcp_servers.yaml` (mounted as Docker volume)
- Each server has: `name`, `url`, `auth_mode` (`bearer` / `none`), optional `prefix`, optional `forward_headers`
- `forward_headers`: list of header names to forward from the original client request to this MCP server (e.g., `["x-tenant-id"]`). Only listed headers are forwarded; all others are filtered out.
- Tools are namespaced with prefix: `servername__toolname` (double underscore separator)
- OPA policy `pb.proxy.mcp_servers_allowed` controls which servers each role can access
- `ToolInjector` discovers tools from all configured servers, merges with prefix dedup

**Dual-mode model routing:**
- **Aliases** — short names from `litellm_config.yaml` (e.g., `"claude-opus"` → `anthropic/claude-opus-4-20250514`)
- **Passthrough** — any `provider/model` format (e.g., `"anthropic/claude-3-5-haiku-20241022"`) routes directly via LiteLLM without config entries

API key resolution: LLM provider keys resolved per-provider via `key_source` config (central/user/hybrid). Default: env vars / Docker Secrets.

Endpoints:
- `GET /v1/models` — Lists configured model aliases (OpenAI-compatible)
- `POST /v1/chat/completions` — Chat endpoint with auth + tool injection + agent loop
- `GET /health` — Health check

Supports SSE streaming (`"stream": true`).
OPA policies (`pb.proxy`) control: provider access, required tools, max iterations, MCP server access.
Configuration: `pb-proxy/litellm_config.yaml` for aliases + `provider_keys`, `pb-proxy/mcp_servers.yaml` for MCP servers.

## Development

### Prerequisites
- Docker + Docker Compose
- Access to a Git server for policy repos (optional, any: Forgejo, GitHub, GitLab, etc.)
- Git server API token with `read:repository` permission (optional)

### First Start
```bash
# Automated (recommended):
./scripts/quickstart.sh

# Or manually:
cp .env.example .env
# Edit .env: PG_PASSWORD (and optionally FORGEJO_URL for Git server integration)

docker compose --profile local-llm --profile local-reranker up -d

# Pull embedding model
docker exec pb-ollama ollama pull nomic-embed-text

# Create Qdrant collections
for col in pb_general pb_code pb_rules; do
  curl -s -X PUT "http://localhost:6333/collections/$col" \
    -H 'Content-Type: application/json' \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}'
done
```

### Production with TLS
```bash
# Set DOMAIN in .env, then:
docker compose --profile tls up -d
```

### Healthchecks
```bash
curl http://localhost:6333/healthz        # Qdrant
curl http://localhost:8181/health          # OPA
curl http://localhost:8082/health          # Reranker
curl http://localhost:11434/api/tags       # Ollama
```

### OPA Policy Tests
```bash
# Run all OPA tests (85 tests: access, privacy, rules, summarization, proxy)
docker exec pb-opa /opa test /policies/ -v

# Evaluate a specific policy
docker exec pb-opa /opa eval \
  -d /policies/pb/access.rego \
  -i '{"agent_role":"analyst","classification":"internal","action":"read"}' \
  'data.pb.access.allow'
```

### MCP Server Tests
```bash
cd mcp-server && python3 -m pytest tests/ -v
```

### E2E Smoke Tests
Full-stack integration tests that start Docker Compose, seed data, and verify critical paths
(auth, search pipeline, OPA policy, PII pseudonymization, knowledge graph).

```bash
# Requires Docker running — starts/stops full stack automatically
RUN_INTEGRATION_TESTS=1 python3 -m pytest tests/integration/e2e/ -v

# Run a single test class
RUN_INTEGRATION_TESTS=1 python3 -m pytest tests/integration/e2e/test_smoke.py::TestSearchPipeline -v
```

Tests are gated behind `RUN_INTEGRATION_TESTS=1` and take ~90s (plus stack startup on first run).
The `docker_stack` fixture calls `docker compose down -v` before and after the test session.

### CI / PR Validation
PR workflow (`.github/workflows/pr-validate.yml`) runs on every PR to `master`:
- **unit-tests** — All service tests in `python:3.12-slim` container (`-m "not integration"`), coverage threshold 80% (`--cov-fail-under=80`)
- **opa-tests** — OPA policy tests (`opa test opa-policies/`)
- **docker-build** — Build all 5 images (no push)
- **security-scan** — `pip-audit` (dependency vulnerabilities) + `bandit` (static analysis), non-blocking

All jobs must pass before merge. Branch protection requires PR — no direct pushes to master.

### Load Tests
Locust-based load tests for the MCP search pipeline (not in CI, local only):
```bash
pip install locust
locust -f tests/load/locustfile.py --host=http://localhost:8080
# Web UI at http://localhost:8089
```

Run tests locally (same as CI):
```bash
docker run --rm -v "$(pwd):/app" -w /app python:3.12-slim bash -c "
  pip install -q -r requirements-dev.txt \
    -r mcp-server/requirements.txt \
    -r ingestion/requirements.txt \
    -r pb-proxy/requirements.txt \
    fastapi uvicorn pydantic prometheus-client pyyaml python-dotenv &&
  PYTHONPATH=.:mcp-server:ingestion:reranker:pb-proxy \
  python -m pytest -m 'not integration' --tb=short -q \
    --cov=shared --cov=mcp-server --cov=ingestion \
    --cov=reranker --cov=pb-proxy --cov=worker \
    --cov-report=term-missing:skip-covered \
    --cov-fail-under=80
"
```

### Performance Caches (T1)
- **Embedding Cache** — In-process TTL cache (`shared/embedding_cache.py`). SHA-256 key of `model:text`. Configurable via `EMBEDDING_CACHE_SIZE` (default 2048), `EMBEDDING_CACHE_TTL` (default 3600s), `EMBEDDING_CACHE_ENABLED`.
- **OPA Result Cache** — TTL cache for `check_opa_policy()` in MCP server. Key: `(role, classification, action)`. Only `pb.access.allow` is cached (deterministic). Configurable via `OPA_CACHE_TTL` (default 60s), `OPA_CACHE_ENABLED`.
- **Batch Embedding** — `EmbeddingProvider.embed_batch()` sends multiple texts in one `/v1/embeddings` request. Used by ingestion pipeline with cache-aware partial batching.

### Structured Telemetry
All 4 services (mcp-server, proxy, reranker, ingestion) share a common telemetry module (`shared/telemetry.py`):

- **OTel Tracing** — `init_telemetry(service_name)` creates TracerProvider + OTLP exporter to Tempo. Auto-instrumentation for FastAPI and httpx (W3C `traceparent` propagation). Configurable via `OTEL_ENABLED` (default `true`), `OTLP_ENDPOINT` (default `http://tempo:4317`).
- **Per-Request Telemetry** — `RequestTelemetry` + `PipelineStep` dataclasses accumulate timing breakdown per request. `trace_operation()` context manager creates OTel span and records step simultaneously. Responses include `_telemetry` block when `TELEMETRY_IN_RESPONSE=true` (default).
- **JSON Metrics Endpoint** — Each service exposes `GET /metrics/json` returning structured metrics from Prometheus registry via `MetricsAggregator`. Designed for demo-UI consumption without PromQL knowledge.
- **Graceful degradation** — If OTel packages not installed or exporter unreachable, tracing silently disables. Prometheus metrics always available.

## Completed Features

1. ✅ **Reranking** — Cross-Encoder service
2. ✅ **Knowledge Graph** — Apache AGE
3. ✅ **Evaluation + Feedback Loop** — `init-db/004_evaluation.sql`, MCP tools `submit_feedback`/`get_eval_stats`
4. ✅ **Knowledge Versioning** — `init-db/005_versioning.sql`, `ingestion/snapshot_service.py`
5. ✅ **Monitoring** — Prometheus + Grafana + Tempo
6. ✅ **Context Summarization** — OPA-controlled, LLM-powered (`pb.summarization` policy)
7. ✅ **Docker Secrets** — `/run/secrets/` with env var fallback
8. ✅ **TLS Profile** — Optional Caddy reverse proxy (`docker compose --profile tls up`)
9. ✅ **AI Provider Proxy** — Optional LLM gateway with transparent tool injection (`docker compose --profile proxy`)
10. ✅ **Chat-Path PII Protection** — Reversible pseudonymization in proxy chat path (`pb-proxy/pii_middleware.py`, OPA-controlled)
11. ✅ **Proxy Model Discovery** — `GET /v1/models` endpoint for OpenAI-compatible client integration
12. ✅ **Proxy SSE Streaming** — Simulated streaming via SSE chunks for `stream: true` requests
13. ✅ **Passthrough Routing** — Dual-mode model routing: aliases via Router + `provider/model` passthrough via direct LiteLLM
14. ✅ **LLM Provider Abstraction** — OpenAI-compatible provider layer (`shared/llm_provider.py`), configurable backends for embedding + summarization, optional GPU stack (vLLM + TEI)
15. ✅ **Context Layers (L0/L1/L2)** — Pre-computed abstracts (L0, ~100 tokens) and overviews (L1, ~500 tokens) at ingestion, `layer` param on search, `get_document` tool for drill-down (progressive loading, no separate OPA policy — access controlled by `pb.access`)
16. ✅ **Proxy Authentication** — ASGI middleware (`pb-proxy/middleware.py`) for global `pb_` API-key auth, identity propagation to MCP servers
17. ✅ **Multi-MCP-Server Aggregation** — Proxy aggregates tools from N MCP servers with per-server auth, prefix namespacing, and OPA-controlled access (`pb.proxy.mcp_servers_allowed`)
18. ✅ **T1 Production Hardening** — Embedding cache (in-process LRU), batch embedding API, OPA result cache, configurable PG pool sizes, Docker health checks for all services
19. ✅ **Structured Telemetry** — Shared OTel module (`shared/telemetry.py`), per-request `_telemetry` in search/chat responses, `/metrics/json` endpoints on all 4 services (mcp-server, proxy, reranker, ingestion), W3C traceparent propagation via auto-instrumented httpx
20. ✅ **Per-Provider Key Management** — Flexible LLM API key resolution (central/user/hybrid modes) via `provider_keys` in `litellm_config.yaml`, `X-Provider-Key` header support
21. ✅ **PII Scan Observability & Strict Defaults** — `PII_SCAN_FORCED` defaults to `true` (fail-closed). Telemetry step `pii_pseudonymize` includes `mode`, `entities_found`, `entity_types`, `fail_mode`. OPA policy `pb.proxy.pii_scan_forced` defaults to `true`, admin can override via `pii_scan_forced_override: false`
22. ✅ **Reranker Provider Abstraction** — Configurable reranker backend via `RERANKER_BACKEND` env var (`powerbrain`/`tei`/`cohere`). Strategy pattern in `shared/rerank_provider.py`, transparent format translation, graceful fallback preserved
23. ✅ **Data-Driven OPA Policies** — All business data (access matrix, purposes, retention, field redaction, pricing/workflow/compliance rules, PII entity types, proxy config) extracted from Rego into `opa-policies/pb/data.json`. Rego files contain only logic, data is configurable via JSON without Rego knowledge. JSON Schema validation (`policy_data_schema.json`). Full OPA test coverage: 85 tests across all 5 policy packages.
24. ✅ **Graph Query PII Masking (B-30)** — `graph_query` and `graph_mutate` results PII-scanned via ingestion `/scan` endpoint before returning. Recursive walker masks firstname, lastname, email, phone, name. Graceful degradation on scanner failure.
25. ✅ **Metadata PII Redaction (B-31)** — `search_knowledge` and `get_code_context` redact PII-sensitive metadata keys based on configurable mapping (`pii_metadata_fields` in `pii_config.yaml`) + OPA `fields_to_redact` policy. Fail-closed on OPA failure.
26. ✅ **Policy Management Tool (B-12)** — `manage_policies` MCP tool with list/read/update actions for OPA policy data sections at runtime. JSON Schema validation before writes, cache invalidation, audit logging with old+new values.
27. ✅ **Correction Boost in Reranking (B-13)** — New `boost_corrections` parameter in `rerank_options`. Documents with `metadata.isCorrection: true` receive a configurable score boost in the heuristic post-rerank phase.
28. ✅ **OPAL Integration (B-10)** — opal-server + opal-client as Docker Compose profile (`--profile opal`). Watches a git repo for policy changes and pushes to OPA in real-time via WebSocket. Configurable via `OPAL_POLICY_REPO_URL`.
29. ✅ **GitHub Adapter** — First source adapter. Syncs GitHub repos into KB with incremental updates (commit SHA tracking). Configurable include/exclude patterns, PAT + GitHub App auth. Polling via pb-worker + `POST /sync/{repo}` endpoint for manual/webhook triggers. All content flows through full pipeline (PII, OPA, quality gate, embedding). Removed files cascade-delete across Qdrant, PG, vault, graph. Config: `ingestion/repos.yaml`.

30. ✅ **Office 365 Adapter** — Second source adapter. Syncs SharePoint, OneDrive, Outlook Mail, Teams Messages, and OneNote into KB via Microsoft Graph API. Delta Queries for incremental sync (except OneNote: timestamp-based). OAuth2 Client Credentials (app-only) + Delegated Auth (OneNote, post-March-2025). Content extraction via Microsoft `markitdown`. Site-level classification in YAML. Teams-SharePoint deduplication (file attachments as refs only). Resource Unit budget tracking + `$batch` API. Config: `ingestion/office365.yaml`.

31. ✅ **Shared Document Extraction + Chat Attachments** — `ContentExtractor` lifted into `ingestion/content_extraction/` (markitdown + python-docx/openpyxl/python-pptx/BeautifulSoup fallbacks). New `POST /extract` endpoint on the ingestion service converts base64-encoded documents (PDF/DOCX/XLSX/PPTX/MSG/EML/RTF/...) to text. The pb-proxy chat path (`/v1/chat/completions` and `/v1/messages`) extracts attached files from multimodal message content before PII scanning and LLM forwarding — both OpenAI `file`/`input_file` blocks and Anthropic `document` blocks are supported. The GitHub adapter can optionally ingest Office documents via `allow_documents: true` in `repos.yaml` (default off; ingested with `source_type="github-document"`). OPA-gated via new `pb.proxy.documents` policy section (allowed roles, max bytes, mime allowlist, max files per request). Optional Tesseract OCR fallback for scanned PDFs via `OCR_FALLBACK_ENABLED` + `WITH_OCR=true` Docker build arg (default off). Office 365 adapter switches to a thin shim that re-exports from the shared package — fully backward compatible.

32. ✅ **Decision-Maker Sales-Demo Package** — Opt-in Streamlit app `pb-demo` (port 8095, profile `demo`) with three tabs showcasing the differentiators: (A) role-aware search with side-by-side analyst/viewer columns, (B) live PII vault scan/ingest/reveal with HMAC-signed tokens, (C) NovaTech org-chart via `graph_query get_neighbors` rendered through `streamlit-agraph`. Backed by two pre-seeded demo keys in `init-db/010_api_keys.sql` (`pb_demo_analyst_localonly`, `pb_demo_viewer_localonly`), 6 German-PII customer records (`testdata/documents_pii.json`), and an 8-employee graph seed (`testdata/graph_seed.json` → `scripts/seed_graph.py`). Quickstart polished: auto-generates passwords, drops the manual-edit block, runs a post-seed smoke query, advertises Demo UI/Grafana/MCP endpoints. New `--seed` / `--demo` flags. Plus migration `init-db/020_viewer_role.sql` widens the `agent_role` CHECK to accept `viewer`, and `docs/playbook-sales-demo.md` provides a 15-min presenter narrative.

33. ✅ **Editions (Community vs Enterprise) + Vault Resolution for Chat** — Every service advertises `"edition": "community"` on `mcp-server` / `"edition": "enterprise"` on `pb-proxy` through `/health` + `/transparency`. New mcp-server endpoint `POST /vault/resolve` does text-level de-pseudonymisation (regex-extract `[ENTITY_TYPE:hash]` → SQL lookup in `pii_vault.pseudonym_mapping` → hash-match against `original_content.pii_entities` → `check_opa_vault_access` per document classification + data_category → `vault_fields_to_redact` per purpose → `log_vault_access`). The pb-proxy agent loop calls it after every tool result under the OPA-gated `pb.proxy.pii_resolve_tool_results` policy (enabled/allowed_roles/allowed_purposes/default_purpose), surfacing stats via `X-Proxy-Vault-Resolved` headers and a `_proxy.vault_resolutions` block in the response. Client declares purpose via `X-Purpose` header (OpenAI-compat extension). Demo Tab D "MCP vs Proxy" renders both paths side-by-side on the same query so decision-makers see the edition effect directly. Docs: `docs/editions.md` with capability matrix + deployment topology.

34. ✅ **Pipeline Inspector (Demo Tab E) + `/preview` endpoint** — New dry-run endpoint `POST /preview` on the ingestion service runs the full pipeline (optional extract from base64 → Presidio scan → quality-score + OPA ingestion gate → OPA privacy decision) without persisting to PostgreSQL or Qdrant. Returns a structured `{extract, scan, quality, privacy, summary}` payload with per-phase timings. Demo Tab E renders the phases as a narrative with fixture docs representing the main adapter types (`demo/fixtures/sharepoint_rahmenvertrag.md`, `outlook_support_request.txt`, `github_readme.md`) plus optional file upload. Classification / source_type / legal_basis are editable per run so a presenter can toggle between `encrypt_and_store` (vault) and `block` (missing legal basis) live. 8 new unit tests in `ingestion/tests/test_preview_endpoint.py` cover the contract + validation + quality-gate + privacy-action paths.

35. ✅ **Semantic PII Verifier** — Optional precision layer that sits between Presidio's `scan_text` output and the rest of the pipeline. Presidio is excellent at recall but flags German compound nouns (`Zahlungsstatus`, `Geschäftsführer`, `Sparkasse Köln`) as PERSON/LOCATION. The verifier catches those false positives without touching recall. New `shared/pii_verify_provider.py` abstraction (same factory pattern as `rerank_provider.py`) with two backends: `noop` (community default, pass-through) and `llm` (OpenAI-compatible chat, e.g. Ollama/qwen2.5:3b). Pattern types (IBAN, email, phone, DOB) skip the verifier — their Presidio score is already trustworthy. Ambiguous types batch into a single low-temperature chat call per document with ±60-char context windows. Fail-open on any error: unreachable LLM, malformed JSON, timeout → keep every candidate Presidio generated. OPA-policy-driven backend via `pb.config.ingestion.pii_verifier.{enabled,backend,min_confidence_keep}` so admins flip runtime behaviour through `manage_policies` without restarting ingestion. Prometheus: `pb_ingestion_pii_verifier_calls_total{entity_type,backend,result}` + `pb_ingestion_pii_verifier_duration_seconds{backend}`. Applied both in the production `ingest_text_chunks` per-chunk loop and in the `/preview` dry-run so demo Tab E can render `{input, forwarded, reviewed, kept, reverted}` stats live, with a `verifier.before` snapshot for contrast. Live verification on the NovaTech SharePoint fixture: 9 raw Presidio candidates → 6 after verifier (3 false positives removed) in ~12 s on qwen2.5:3b CPU. Docs: `docs/pii-verifier.md` (architecture + configuration) + `docs/pii-custom-model.md` (long-horizon roadmap for a fine-tuned German PII model — triggers, phases, why we're not building it today).

36. ✅ **Edition Boundary Transparency** — Made the chat-path bypass on Anthropic consumer plans (Pro/Max) explicit at every customer-facing touchpoint. New `docs/compliance-claude-desktop.md` one-pager with the three-tier mitigation model (real-time proxy / detective chat-history ingest / endpoint DLP), DPA vs EU AI Act distinction, scenario recommendations and DPO question list. `docs/editions.md` gained a "Edition boundary" section with the three-data-paths matrix (ingest / tool calls / chat content), explicitly stating that ingest + tool calls are protected in both editions and only the free chat channel is bypassed without `pb-proxy`. `docs/gdpr-external-ai-services.md` DPA table broken down by plan, assessment matrix extended with Powerbrain configurations. CLAUDE.md, README.md, `docs/getting-started.md` cross-link to the new docs. Tonfall sachlich-neutral im Hauptdokument, warnend (⚠️) nur in der GDPR-Doku. PR #153.

37. ✅ **Privacy Incident MCP Tools (B-47, GDPR Art. 33/34)** — Five MCP tools surface the existing `privacy_incidents` schema: `report_breach` (any role; agents may flag detections), `list_incidents` (admin; `attention=true` uses `v_incidents_requiring_attention` view), `assess_incident` (admin; OPA-driven risk-score with `force_notifiable`/`force_not_notifiable` overrides; falls through to `under_review` or `false_positive`), `notify_authority` (admin; records Art. 33 notification with method + ref in `containment_actions.authority_notification`), `notify_data_subject` (admin; first call moves status, later calls append to `containment_actions.subject_notifications` ledger). OPA package `pb.incidents` with data-driven risk scoring (high/medium/low PII weights × subject brackets × category multiplier × notifiable_threshold), all configurable via `pb.config.incidents` in `data.json`. New worker job `incident_deadline_check` (every 15 min) classifies open incidents into `warning`/`critical`/`overdue` buckets via Prometheus gauges; three alert rules in `monitoring/alerting_rules.yml` (`IncidentAssessmentOverdue`, `IncidentNotificationDeadlineImminent`, `IncidentNotificationOverdue`) cover the 24h/48h/72h deadline. 19 MCP-tool tests + 21 OPA tests. Powerbrain documents the evidence chain — outbound notification (email to authority, letter to subject) remains an organisational workflow.

Details on all features: see `docs/architecture.md`

## Code Conventions

- Python 3.12+, type hints everywhere
- Async/await for all I/O operations
- Pydantic models for request/response
- Rego policies in `opa-policies/pb/` with package `pb.*`, data-driven via `data.json`
- SQL migrations numbered: `001_schema.sql`, `002_privacy.sql`, ...
- Docker images: multi-stage where useful, Alpine-based where possible
- Environment variables for all configuration (no hardcoded values)
- Graceful degradation: every service must work without the reranker
- Docker Secrets supported via `_read_secret()` with env var fallback

### Naming Prefix Convention (`pb`)

All project-specific identifiers use the `pb` (Powerbrain) prefix consistently:

| Category | Pattern | Examples |
|---|---|---|
| Container names | `pb-<service>` | `pb-qdrant`, `pb-mcp-server`, `pb-proxy` |
| Docker network | `pb-net` | |
| API token prefix | `pb_` | `pb_dev_localonly_...`, `pb_<key>` |
| OPA packages | `pb.<domain>` | `pb.access`, `pb.proxy`, `pb.privacy` |
| OPA data paths | `/v1/data/pb/` | `/v1/data/pb/access/allow` |
| DB user | `pb_admin` | |
| DB name | `powerbrain` | |
| Prometheus metrics | `pb_<service>_` | `pb_mcp_requests_total`, `pb_reranker_duration_seconds` |
| Qdrant collections | `pb_<type>` | `pb_general`, `pb_code`, `pb_rules` |
| Logger names | `pb-<component>` | `pb-mcp`, `pb-ingestion`, `pb-graph` |
| Git repos | `pb-<name>` | `pb-policies`, `pb-schemas` (any Git server) |

### Plans and Specs

Implementation plans and design specs are stored centrally:

- **Plans:** `docs/plans/YYYY-MM-DD-<feature-name>.md`
- **Specs:** `docs/specs/YYYY-MM-DD-<feature-name>.md`

## Key Decisions

| Decision | Chosen | Alternatives | Reason |
|---|---|---|---|
| Vector DB | Qdrant | Milvus, ChromaDB | Best perf + filtering + clustering |
| Embedding | nomic-embed-text (768d) | mxbai-embed-large | Quality/speed balance |
| Reranker | ms-marco-MiniLM-L-6-v2 | bge-reranker-v2-m3 | Fast; multilingual as option |
| Reranker Abstraction | Strategy pattern (`shared/rerank_provider.py`) | Hardcoded HTTP call | Supports TEI, Cohere, custom backends |
| Summarization | qwen2.5:3b (Ollama) | llama3.2:3b | Small, fast, good instruction following |
| Policy Engine | OPA (Rego) | Cerbos, GoRules | CNCF standard, battle-tested |
| PII Scanner | Presidio | spaCy NER | Broad entity detection + extensible |
| Git Server | Any (Forgejo default) | — | Supports Forgejo, GitHub, GitLab, Gitea, Bitbucket |
| Relational DB | PostgreSQL 16 | MySQL, SQLite | JSONB, GIN index, extensions |
| PII Storage | Sealed Vault (Dual) | Destructive masking, full encryption | Reversible, searchable, GDPR-compliant |
| TLS | Caddy (optional profile) | Nginx, Traefik | Zero-config HTTPS, simple Caddyfile |
| Secrets | Docker Secrets + env fallback | Vault, SOPS | Simple, no extra infrastructure |
| LLM Provider | OpenAI-compat (`shared/llm_provider.py`) | Direct Ollama API | Supports vLLM, TEI, infinity, any OpenAI-compat |

## Pre-Public Checklist

Tasks completed for open-sourcing the repository:

- [x] **Audit secrets and internal URLs** — Parameterized `build-images.sh`, sanitized doc paths
- [x] **Review `.env.example`** — No real credentials or internal hostnames
- [x] **Add LICENSE file** — Apache 2.0
- [x] **Dual CI** — `.forgejo/` (internal) + `.github/` (public) coexist
- [x] **GitHub Actions CI** — `.github/workflows/pr-validate.yml` with 4 jobs (unit-tests, opa-tests, docker-build, security-scan)
- [x] **Branch protection on `master`** — Require PR + status checks
- [x] **CONTRIBUTING.md** — Contributor guide with dev setup, test commands, code conventions
- [x] **SECURITY.md** — Vulnerability reporting policy via GitHub Security Advisories
- [x] **GitHub Templates** — Issue templates (bug report, feature request) + PR template
- [x] **README badges** — CI status, License, Docker, MCP
- [x] **Quick Start script** — `scripts/quickstart.sh` for automated first-time setup
- [x] **Getting Started guide** — `docs/getting-started.md` — tutorial for newcomers
- [x] **MCP Tool Reference** — `docs/mcp-tools.md` — all 28 tools documented
- [x] **Coverage threshold** — 80% minimum enforced in CI (`--cov-fail-under=80`)
- [x] **Security scanning** — `pip-audit` + `bandit` in CI (non-blocking)
- [x] **Load tests** — Locust-based load test for search pipeline (`tests/load/`)
- [x] **Set repo description + topics** — Description, topics (mcp, rag, opa, gdpr, etc.)
- [x] **Switch to public** — `gh repo edit --visibility public`
