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
│  MCP Server (FastAPI)                           │
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
│   ├── llm_provider.py    ← OpenAI-compat LLM provider abstraction
│   ├── telemetry.py       ← OTel init, trace_operation, MetricsAggregator
│   └── tests/
│       └── test_llm_provider.py
├── mcp-server/
│   ├── server.py          ← MCP Server (10 tools)
│   ├── graph_service.py   ← Knowledge Graph (Apache AGE)
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
│   ├── Dockerfile
│   └── requirements.txt
├── init-db/
│   ├── 001_schema.sql     ← Base schema
│   ├── 002_privacy.sql    ← Privacy extensions
│   ├── 003_knowledge_graph.sql ← Apache AGE graph setup
│   └── 007_pii_vault.sql  ← Sealed Vault (PII originals + mappings)
├── opa-policies/pb/
│   ├── access.rego         ← Access control
│   ├── rules.rego          ← Business rules
│   ├── privacy.rego        ← GDPR policies
│   ├── summarization.rego  ← Context summarization policies
│   └── proxy.rego          ← Proxy policies (provider access, MCP server ACL)
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
│   ├── tool_injection.py  ← Multi-server MCP tool discovery + merge
│   ├── agent_loop.py      ← Tool-call execution loop with server routing
│   ├── mcp_config.py      ← MCP server config model + YAML loader
│   ├── config.py          ← Configuration
│   ├── litellm_config.yaml← LLM provider config
│   ├── mcp_servers.yaml   ← MCP server connections (name, URL, auth)
│   ├── Dockerfile
│   └── requirements.txt
└── docs/
    ├── what-is-powerbrain.md       ← Detailed overview and positioning
    ├── deployment.md               ← Dev, prod, TLS, Docker Secrets guide
    ├── architektur.md              ← Technical deep-dive (components, GDPR)
    ├── KNOWN_ISSUES.md             ← Resolved issues archive (P0–P3)
    ├── technologie-entscheidungen.md ← ADRs (VLM, vLLM, Git adapter, OTel)
    ├── skalierbarkeit.md           ← Scaling, load balancing, caching
    └── dsgvo-externe-ki-dienste.md ← Legal assessment for external LLMs
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
| ollama        | 11434 | Ollama                             | Local embeddings + summarization |
| vllm          | 8000  | vLLM (optional, `gpu` profile)     | Production LLM serving           |
| tei           | 8010  | HF TEI (optional, `gpu` profile)   | Production embedding serving     |
| caddy         | 80/443| Caddy 2 (optional, `tls` profile)  | TLS reverse proxy                |
| forgejo       | 3000  | Forgejo (external, not in Compose) | Git repos, policies, schemas     |
| prometheus    | 9090  | Prometheus                         | Metrics collection               |
| grafana       | 3001  | Grafana                            | Dashboards + visualization       |
| tempo         | 4317  | Grafana Tempo                      | Distributed tracing              |
| pb-proxy      | 8090  | Python, FastAPI, LiteLLM, MCP SDK    | AI Provider Proxy (optional)     |

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
3. **Cross-Encoder** scores query-document relevance, returns top-k

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

### MCP Tools (11)
- `search_knowledge` — Semantic search (Qdrant + reranking); supports `summarize` and `summary_detail` parameters; optional PII originals via vault token
- `query_data` — Structured queries (PostgreSQL)
- `get_rules` — Business rules for a context
- `check_policy` — OPA policy evaluation
- `ingest_data` — Ingest new data
- `get_classification` — Classification lookup
- `list_datasets` — List available datasets
- `get_code_context` — Code search (Qdrant + reranking); supports `summarize` and `summary_detail` parameters
- `graph_query` — Knowledge graph queries (nodes, relationships, paths)
- `graph_mutate` — Knowledge graph mutations (developer/admin only)
- `get_document` — Retrieve document by ID at specific context layer (L0/L1/L2) for progressive loading

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
- `forgejo_token.txt` — Forgejo API token

Services read from `/run/secrets/<name>` with env var fallback for backward compatibility.
The `_read_secret()` helper checks `<ENV_VAR>_FILE` first, then falls back to `<ENV_VAR>`.

### Forgejo Integration
No separate git container — uses existing Forgejo:
- `pb-policies` repo → OPA bundle polling
- `pb-schemas` repo → JSON schema validation
- `pb-docs` + project repos → Ingestion pipeline

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
- `ProxyKeyVerifier` (`pb-proxy/auth.py`) validates against PostgreSQL `api_keys` table
- Identity propagation: user's `pb_` key is forwarded to MCP servers (each tool call authenticated as the user)
- LLM provider keys come from central config (env vars / Docker Secrets), never from user tokens

**Multi-MCP-Server Aggregation:**
- Configured via `pb-proxy/mcp_servers.yaml` (mounted as Docker volume)
- Each server has: `name`, `url`, `auth_mode` (`bearer` / `none`), optional `prefix`
- Tools are namespaced with prefix: `servername__toolname` (double underscore separator)
- OPA policy `pb.proxy.mcp_servers_allowed` controls which servers each role can access
- `ToolInjector` discovers tools from all configured servers, merges with prefix dedup

**Dual-mode model routing:**
- **Aliases** — short names from `litellm_config.yaml` (e.g., `"claude-opus"` → `anthropic/claude-opus-4-20250514`)
- **Passthrough** — any `provider/model` format (e.g., `"anthropic/claude-3-5-haiku-20241022"`) routes directly via LiteLLM without config entries

API key resolution: LLM provider keys from env vars / Docker Secrets (NOT from user Bearer tokens).

Endpoints:
- `GET /v1/models` — Lists configured model aliases (OpenAI-compatible)
- `POST /v1/chat/completions` — Chat endpoint with auth + tool injection + agent loop
- `GET /health` — Health check

Supports SSE streaming (`"stream": true`).
OPA policies (`pb.proxy`) control: provider access, required tools, max iterations, MCP server access.
Configuration: `pb-proxy/litellm_config.yaml` for aliases, `pb-proxy/mcp_servers.yaml` for MCP servers.

## Development

### Prerequisites
- Docker + Docker Compose
- Access to existing Forgejo server (optional)
- Forgejo API token with `read:repository` permission (optional)

### First Start
```bash
cp .env.example .env
# Edit .env: PG_PASSWORD (and optionally FORGEJO_URL, FORGEJO_TOKEN)

docker compose up -d

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
# Run all OPA tests (including summarization)
docker exec pb-opa /opa test /policies/pb/ -v

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
16. ✅ **Proxy Authentication** — API-key auth for proxy (`pb-proxy/auth.py`), identity propagation to MCP servers
17. ✅ **Multi-MCP-Server Aggregation** — Proxy aggregates tools from N MCP servers with per-server auth, prefix namespacing, and OPA-controlled access (`pb.proxy.mcp_servers_allowed`)
18. ✅ **T1 Production Hardening** — Embedding cache (in-process LRU), batch embedding API, OPA result cache, configurable PG pool sizes, Docker health checks for all services
19. ✅ **Structured Telemetry** — Shared OTel module (`shared/telemetry.py`), per-request `_telemetry` in search/chat responses, `/metrics/json` endpoints on all 4 services (mcp-server, proxy, reranker, ingestion), W3C traceparent propagation via auto-instrumented httpx

Details on all features: see `docs/architektur.md`

## Code Conventions

- Python 3.12+, type hints everywhere
- Async/await for all I/O operations
- Pydantic models for request/response
- Rego policies in `opa-policies/pb/` with package `pb.*`
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
| Forgejo org/repos | `pb-org/pb-<name>` | `pb-org/pb-policies`, `pb-org/pb-schemas` |

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
| Summarization | qwen2.5:3b (Ollama) | llama3.2:3b | Small, fast, good instruction following |
| Policy Engine | OPA (Rego) | Cerbos, GoRules | CNCF standard, battle-tested |
| PII Scanner | Presidio | spaCy NER | Broad entity detection + extensible |
| Git Server | Forgejo (external) | Gitea | Already available, API-compatible |
| Relational DB | PostgreSQL 16 | MySQL, SQLite | JSONB, GIN index, extensions |
| PII Storage | Sealed Vault (Dual) | Destructive masking, full encryption | Reversible, searchable, GDPR-compliant |
| TLS | Caddy (optional profile) | Nginx, Traefik | Zero-config HTTPS, simple Caddyfile |
| Secrets | Docker Secrets + env fallback | Vault, SOPS | Simple, no extra infrastructure |
| LLM Provider | OpenAI-compat (`shared/llm_provider.py`) | Direct Ollama API | Supports vLLM, TEI, infinity, any OpenAI-compat |
