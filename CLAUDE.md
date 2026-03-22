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
 Ollama
 (embeddings + summarization)
```

## Directory Structure

```
powerbrain/
├── CLAUDE.md              ← You are here
├── README.md              ← Quick start and overview
├── docker-compose.yml     ← All services
├── .env.example           ← Environment variables
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
│   ├── retention_cleanup.py ← GDPR retention cleanup jobs
│   ├── Dockerfile
│   └── requirements.txt
├── init-db/
│   ├── 001_schema.sql     ← Base schema
│   ├── 002_privacy.sql    ← Privacy extensions
│   ├── 003_knowledge_graph.sql ← Apache AGE graph setup
│   └── 007_pii_vault.sql  ← Sealed Vault (PII originals + mappings)
├── opa-policies/kb/
│   ├── access.rego         ← Access control
│   ├── rules.rego          ← Business rules
│   ├── privacy.rego        ← GDPR policies
│   └── summarization.rego  ← Context summarization policies
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
│   ├── tool_injection.py  ← MCP tool discovery + merge
│   ├── agent_loop.py      ← Tool-call execution loop
│   ├── config.py          ← Configuration
│   ├── litellm_config.yaml← LLM provider config
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
- `kb.summarization.summarize_allowed` — all roles except viewer may request summaries
- `kb.summarization.summarize_required` — confidential data: only summaries, never raw chunks
- `kb.summarization.summarize_detail` — restricted data gets `brief` summaries only

Agents use `summarize=true` and `summary_detail` parameters on `search_knowledge` and `get_code_context`.
Response includes `summary` (text) and `summary_policy` (`requested` | `enforced` | `denied`).
Graceful degradation: if Ollama summarization fails → raw chunks returned.

Config: `SUMMARIZATION_MODEL` (default: `qwen2.5:3b`), `SUMMARIZATION_ENABLED` (default: `true`).

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

### MCP Tools (10)
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

### Privacy (GDPR)
- **PII Scanner** (Microsoft Presidio) at ingestion
- **Purpose binding** via OPA policy (`kb.privacy`)
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
- `kb-policies` repo → OPA bundle polling
- `kb-schemas` repo → JSON schema validation
- `kb-docs` + project repos → Ingestion pipeline

### AI Provider Proxy (optional)
Optional gateway activated via `docker compose --profile proxy up`.
Sits between AI consumers and LLM providers:
1. Client sends OpenAI-compatible request to proxy (port 8090)
2. Proxy injects Powerbrain MCP tools into `tools[]` array
3. Forwards augmented request to LLM (via LiteLLM, 100+ providers)
4. When LLM returns tool calls → proxy executes against MCP server
5. Repeats until final response, then returns to client

OPA policies (`kb.proxy`) control: provider access, required tools, max iterations.
Configuration: `pb-proxy/litellm_config.yaml` for LLM provider setup.

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
docker exec kb-ollama ollama pull nomic-embed-text

# Create Qdrant collections
for col in knowledge_general knowledge_code knowledge_rules; do
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
docker exec kb-opa /opa test /policies/kb/ -v

# Evaluate a specific policy
docker exec kb-opa /opa eval \
  -d /policies/kb/access.rego \
  -i '{"agent_role":"analyst","classification":"internal","action":"read"}' \
  'data.kb.access.allow'
```

### MCP Server Tests
```bash
cd mcp-server && python3 -m pytest tests/ -v
```

## Completed Features

1. ✅ **Reranking** — Cross-Encoder service
2. ✅ **Knowledge Graph** — Apache AGE
3. ✅ **Evaluation + Feedback Loop** — `init-db/004_evaluation.sql`, MCP tools `submit_feedback`/`get_eval_stats`
4. ✅ **Knowledge Versioning** — `init-db/005_versioning.sql`, `ingestion/snapshot_service.py`
5. ✅ **Monitoring** — Prometheus + Grafana + Tempo
6. ✅ **Context Summarization** — OPA-controlled, Ollama-powered (`kb.summarization` policy)
7. ✅ **Docker Secrets** — `/run/secrets/` with env var fallback
8. ✅ **TLS Profile** — Optional Caddy reverse proxy (`docker compose --profile tls up`)
9. ✅ **AI Provider Proxy** — Optional LLM gateway with transparent tool injection (`docker compose --profile proxy`)
10. ✅ **Chat-Path PII Protection** — Reversible pseudonymization in proxy chat path (`pb-proxy/pii_middleware.py`, OPA-controlled)

Details on all features: see `docs/architektur.md`

## Code Conventions

- Python 3.12+, type hints everywhere
- Async/await for all I/O operations
- Pydantic models for request/response
- Rego policies in `opa-policies/kb/` with package `kb.*`
- SQL migrations numbered: `001_schema.sql`, `002_privacy.sql`, ...
- Docker images: multi-stage where useful, Alpine-based where possible
- Environment variables for all configuration (no hardcoded values)
- Graceful degradation: every service must work without the reranker
- Docker Secrets supported via `_read_secret()` with env var fallback

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
