# 🧠 Powerbrain

[![CI](https://github.com/nuetzliches/powerbrain/actions/workflows/pr-validate.yml/badge.svg)](https://github.com/nuetzliches/powerbrain/actions/workflows/pr-validate.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docker Compose](https://img.shields.io/badge/Docker_Compose-ready-2496ED?logo=docker)](docker-compose.yml)
[![MCP](https://img.shields.io/badge/MCP-compatible-green)](https://modelcontextprotocol.io/)

Open source. Closed data. 🔐

> *"AI eats context. We decide what's on the menu."*

Open-source context engine that feeds AI agents with policy-compliant enterprise knowledge — self-hosted, GDPR-native, provider-agnostic.

---

## The Problem

Every AI agent needs context. But feeding enterprise data to LLMs means losing control — over who sees what, how long it's retained, and whether it complies with GDPR. Most solutions either block AI entirely or hand everything over. Neither works.

## The Solution

Powerbrain sits between your data and your AI agents. It delivers context through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), with every request checked by a policy engine. Your data stays on your infrastructure. Your policies decide what gets through.

```
Agent / Skill
    │ MCP
    ▼
┌─────────────────────────────────────────────────┐
│  Powerbrain MCP Server                          │
│  ├─ OPA Policy Check (every request)            │
│  ├─ Circuit Breaker + Approval Queue (Art. 14)  │
│  ├─ Qdrant Vector Search (oversampled)          │
│  ├─ Cross-Encoder Reranking (top-k)             │
│  ├─ Context Summarization (policy-controlled)   │
│  ├─ Sealed Vault (PII pseudonymization)         │
│  └─ Tamper-Evident Audit Log (Art. 12)          │
└─────────────────────────────────────────────────┘
    │           │           │           │
    ▼           ▼           ▼           ▼
 Qdrant    PostgreSQL     OPA       Ollama/vLLM/TEI
 (vectors)  (data+vault)  (policies) (embeddings+LLM)
                │
                ▼
         ┌──────────────┐
         │  pb-worker   │  Accuracy metrics, drift
         │ (APScheduler)│  detection, audit retention
         └──────────────┘
```

## ✨ Core Features

🔒 **Policy-Aware Context Delivery** — Every search request is checked against OPA policies. Classification levels (public, internal, confidential, restricted) control what each agent role can access. Compliance is executable code, not documentation.

🛡️ **Sealed Vault & Pseudonymization** — PII is detected at ingestion (Microsoft Presidio), pseudonymized with per-project salts, and stored in a dual-layer vault. Originals require HMAC-signed, time-limited tokens with purpose binding. Art. 17 deletion: remove the vault mapping and pseudonyms become irreversible.

🎯 **Relevance Pipeline** — 3-stage search: Qdrant oversampling (5x candidates) → OPA policy filtering → Cross-Encoder reranking. Graceful degradation: if the reranker is down, results fall back to vector ordering.

📝 **Context Summarization** — Agents can request summaries instead of raw chunks. OPA policies can enforce summarization for sensitive data (confidential = summary only, no raw text), control detail levels, or deny summarization entirely. Powered by any OpenAI-compatible LLM (Ollama, vLLM, or external).

🔌 **MCP-Native Interface** — 23 tools accessible through the Model Context Protocol. Works with any MCP-compatible agent (Claude, OpenCode, custom). One endpoint, one protocol.

🏠 **Self-Hosted & GDPR-Native** — Everything runs on your infrastructure. No external API calls for embeddings, search, or summarization. Docker Compose up and you're running.

🔀 **AI Provider Proxy** — Optional gateway between your AI consumers and their LLM providers. Transparently injects Powerbrain tools into every LLM request and executes tool calls automatically. Your teams use any LLM they prefer (100+ providers via LiteLLM); Powerbrain ensures they always query policy-checked enterprise context. Activate with `docker compose --profile proxy up`.

## ⚖️ EU AI Act Compliance Toolkit

Powerbrain is **not itself a high-risk AI system**, but Deployers who operate one in regulated sectors (finance, healthcare, HR) need infrastructure that delivers the Art. 9–15 capabilities. Powerbrain ships them as executable building blocks, not PDFs:

| Article | Feature | How |
|---|---|---|
| **Art. 9** — Risk management | Concrete risk register + live indicators | [`docs/risk-management.md`](docs/risk-management.md) with 8 identified risks (R-01..R-08). `GET /health` with `Accept: application/json` returns 6 live risk indicators and HTTP 503 when critical. |
| **Art. 10** — Data quality | Blocking ingestion quality gate | Composite score (length, language confidence, PII density, encoding, metadata) with per-source_type thresholds via OPA `pb.ingestion.quality_gate`. Rejected documents are audited in `ingestion_rejections`. |
| **Art. 11** / Annex IV — Technical docs | Admin-triggered Annex IV generator | `generate_compliance_doc` MCP tool renders all 9 Annex IV sections as Markdown from live runtime state (models, OPA policies, collections, audit chain, risk register). |
| **Art. 12** — Logging | Tamper-evident audit hash chain | SHA-256 hash chain on `agent_access_log` via PostgreSQL trigger with advisory locks, append-only enforcement, checkpoint+prune retention that preserves chain continuity. Verify via `verify_audit_integrity`, export via `export_audit_log`. |
| **Art. 13** — Transparency | Auth-required transparency endpoint | `GET /transparency` and `get_system_info` MCP tool expose active models, OPA policies, collection stats, PII scanner config, and audit integrity — with deterministic version fingerprint. |
| **Art. 14** — Human oversight | Global kill-switch + approval queue | `POST /circuit-breaker` halts all data-retrieval tools instantly. Confidential/restricted requests from non-admin roles are intercepted into `pending_reviews`; admins decide via `review_pending`, agents poll via `get_review_status`. |
| **Art. 15** — Accuracy & drift | Windowed feedback metrics + embedding drift detection | Per-collection baseline centroids in `embedding_reference_set`, refreshed every 5 minutes by `pb-worker`. Prometheus gauges + alerts (`QualityDrift`, `HighEmptyResultRate`, `RerankerScoreDrift`, `EmbeddingDriftDetected`), pre-provisioned `pb-accuracy` Grafana dashboard. |

The `pb-worker` maintenance container runs four APScheduler jobs: accuracy metrics refresh (5 min), pending-review timeouts (hourly), GDPR retention cleanup (daily 02:00), audit retention cleanup (daily 03:00).

## 🧩 Editions

Powerbrain ships as two tiers, both Apache-2.0, both self-hosted.
**Community** is the MCP context engine (search, vault, OPA, audit).
**Enterprise** adds `pb-proxy` — an OpenAI-compatible chat gateway that
orchestrates tool-call loops, pseudonymises the wire, and resolves
vault pseudonyms for chat responses per OPA policy. Full capability
matrix and migration notes in [docs/editions.md](docs/editions.md).

Edition detection is on every service's `/health` and `/transparency`
endpoint: `"edition": "community"` on `mcp-server:8080`, `"edition":
"enterprise"` on `pb-proxy:8090`.

## 🎬 Run a Sales Demo in 5 Minutes

Need to show a decision-maker what Powerbrain does? Spin up the full demo stack (role-aware search, live PII vault, knowledge-graph explorer) with a single command:

```bash
./scripts/quickstart.sh --demo
```

The script seeds 21 base documents, 6 customer records with German PII, and an 8-person org-chart graph. When healthchecks finish, open **http://localhost:8095** — a five-tab Streamlit app with inline presenter notes: role-contrast search, live PII vault, knowledge graph, a **MCP vs Proxy** side-by-side that shows the community/enterprise contrast, and a **Pipeline Inspector** that dry-runs any document through the ingestion pipeline. The full 15-minute narrative lives in [docs/playbook-sales-demo.md](docs/playbook-sales-demo.md).

## 🚀 Quick Start

```bash
git clone https://github.com/nuetzliches/powerbrain.git && cd powerbrain

# Automated setup (recommended):
./scripts/quickstart.sh            # base stack, no seed
./scripts/quickstart.sh --seed     # + 21 sample documents
./scripts/quickstart.sh --demo     # + PII vault fixtures + graph + demo UI on :8095

# Or manually:
cp .env.example .env        # PG_PASSWORD comes from secrets/pg_password.txt (auto-generated)
docker compose --profile local-llm --profile local-reranker up -d
docker exec pb-ollama ollama pull nomic-embed-text
for col in pb_general pb_code pb_rules; do
  curl -s -X PUT "http://localhost:6333/collections/$col" \
    -H 'Content-Type: application/json' \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}' && echo " → $col ✓"
done
```

Verify everything is running:

```bash
curl -s http://localhost:8080/health   # MCP Server
curl -s http://localhost:6333/healthz  # Qdrant
curl -s http://localhost:8181/health   # OPA
```

Connect your agent (a default dev key is pre-seeded for local use):

```json
{
  "mcpServers": {
    "powerbrain": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer pb_dev_localonly_do_not_use_in_production"
      }
    }
  }
}
```

That's it. Your agent now has access to `search_knowledge`, `query_data`, `graph_query`, `generate_compliance_doc`, and 19 more tools — see [MCP Tool Reference](docs/mcp-tools.md). For production keys, see the [Getting Started guide](docs/getting-started.md#2-authentication).

### Optional: AI Provider Proxy

```bash
# 1. Uncomment/add your LLM provider in pb-proxy/litellm_config.yaml
# 2. Set API keys in .env (e.g. OPENAI_API_KEY=sk-...)
docker compose --profile proxy up -d

# List available models:
curl http://localhost:8090/v1/models

# Use the proxy — Powerbrain tools are injected automatically:
curl http://localhost:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"What are our GDPR deletion policies?"}]}'
```

## 🔍 How It Works

```
1. Agent calls search_knowledge("GDPR deletion policy", summarize=true)
2. Powerbrain embeds the query (nomic-embed-text via configurable provider)
3. Qdrant returns 50 candidates (10 × 5 oversampling)
4. OPA filters by agent role + data classification → 30 remain
5. Cross-Encoder reranks by query-document relevance → top 10
6. OPA summarization policy: allowed? required? detail level?
7. LLM summarizes the chunks (if applicable)
8. Response: results + summary + policy transparency
```

## 🧭 Principles

1. **Sovereignty by design** — Data sovereignty is not a feature, it's the architecture. No external API calls. No cloud dependencies. Your data, your rules.

2. **Enable, don't restrict** — The goal is not to prevent AI adoption, but to make it safely usable. Powerbrain says "yes, but with guardrails" instead of "no."

3. **Policy as code** — Compliance rules are OPA/Rego policies, version-controlled and testable. Not Word documents. Not checkbox audits.

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [Getting Started](docs/getting-started.md) | Step-by-step tutorial: ingest data, search, understand policies |
| [MCP Tool Reference](docs/mcp-tools.md) | All 23 MCP tools with parameters and access roles |
| [What is Powerbrain?](docs/what-is-powerbrain.md) | Detailed overview and positioning |
| [Architecture](docs/architecture.md) | Technical deep-dive |
| [Deployment Guide](docs/deployment.md) | Dev, production, TLS, Docker Secrets |
| [GitHub Adapter](docs/github-adapter.md) | Sync GitHub repositories into the knowledge base |
| [Office 365 Adapter](docs/office365-adapter.md) | Sync SharePoint, OneDrive, Outlook, Teams, OneNote |
| [Technology Decisions](docs/technology-decisions.md) | ADRs and trade-offs |
| [Risk Register](docs/risk-management.md) | EU AI Act Art. 9 risk register (R-01..R-08) |
| [EU AI Act Plan](docs/plans/2026-04-08-eu-ai-act-compliance.md) | Implementation plan for B-40..B-46 |
| [CLAUDE.md](CLAUDE.md) | Agent-facing reference (tools, schemas, conventions) |

## 🤝 Contributing

Powerbrain is open source ([Apache 2.0](LICENSE)). Contributions welcome — whether it's a new OPA policy, a better reranker model, or documentation improvements.

*Open source. Closed data.* 🔐

## 📄 License

[Apache License 2.0](LICENSE). Dependencies under their respective licenses.

## AI Usage
Parts of this codebase were developed with the assistance of AI coding tools (e.g. Claude, GitHub Copilot). 