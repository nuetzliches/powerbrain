# What is Powerbrain?

Powerbrain is an open-source **context engine** that feeds AI agents with policy-compliant enterprise knowledge. It sits between your data and your AI agents, delivering context through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) with every request checked by a policy engine.

Self-hosted. GDPR-native. Provider-agnostic.

> *"AI eats context. We decide what's on the menu."*

## The Problem

Organizations want to use AI agents against their own data — internal docs, code, policies, customer records. But the moment you connect enterprise data to an AI, three things break:

1. **Data sovereignty** — Who controls what the agent sees? Can you prove it?
2. **Privacy compliance** — PII in search results creates GDPR liability. Masking it destroys search quality.
3. **Provider lock-in** — Building on a vendor's RAG API means your context pipeline is someone else's product.

Most solutions either block AI adoption entirely ("too risky") or hand everything over ("just use the API"). Neither works.

## The Solution

Powerbrain is a self-hosted context delivery layer. It doesn't replace your AI agents — it feeds them. Every search result passes through a policy engine before reaching the agent. Sensitive data is pseudonymized, summarized, or blocked based on executable rules.

```
Agent → MCP → Powerbrain → Policy Check → Context Delivery
                              ↕
                    Qdrant · PostgreSQL · OPA · Ollama/vLLM/TEI
```

## Core Features

### 🔒 Policy-Aware Context Delivery

Every request is checked by [Open Policy Agent](https://www.openpolicyagent.org/) against data classification levels (public, internal, confidential, restricted) and agent roles. Access decisions are Rego policies — version-controlled, testable, deployable without code changes.

### 🛡️ Sealed Vault & Pseudonymization

PII is detected at ingestion using [Microsoft Presidio](https://microsoft.github.io/presidio/), pseudonymized with deterministic per-project salts (preserving sentence structure for better embeddings), and stored in a dual-layer vault. The pseudonymized text goes to Qdrant for search quality; originals stay in a secured PostgreSQL schema with row-level security. Accessing originals requires HMAC-signed, time-limited tokens with purpose binding.

Art. 17 right to erasure: delete the vault mapping and pseudonyms become irreversible — the data is effectively anonymized without touching the vector store.

### 🎯 3-Stage Relevance Pipeline

1. **Qdrant** returns 5x oversampled candidates (50 results for a top-10 query)
2. **OPA** filters by classification + agent role (removes unauthorized results)
3. **Cross-Encoder** reranks remaining results by query-document relevance (returns top-k)

Graceful degradation: if the reranker is down, results fall back to Qdrant's vector ordering.

### 📝 Context Summarization

Agents can request summaries instead of raw chunks. OPA policies control this per classification level:

- **Allowed** — Agent may opt in to summarization
- **Enforced** — Confidential data is only returned as summaries (raw chunks stripped)
- **Denied** — Viewers don't get summarization access

Powered by any OpenAI-compatible LLM (Ollama, vLLM, or external) with configurable models. Falls back to raw chunks if summarization fails.

### 🔌 MCP-Native Interface

23 tools accessible through a single MCP endpoint:

| Category | Tools |
|----------|-------|
| **Search & Retrieval** | `search_knowledge`, `get_code_context`, `get_document` (progressive loading via L0/L1/L2 context layers) |
| **Structured Data** | `query_data`, `list_datasets`, `get_classification` |
| **Data Management** | `ingest_data`, `delete_documents` |
| **Knowledge Graph** | `graph_query` (traverse, find paths), `graph_mutate` (developer/admin) |
| **Policy & Rules** | `check_policy`, `get_rules`, `manage_policies` (runtime OPA config, admin) |
| **Evaluation** | `submit_feedback` (1-5 star ratings), `get_eval_stats` (quality metrics) |
| **Snapshots** | `create_snapshot`, `list_snapshots` |
| **EU AI Act** | `generate_compliance_doc` (Annex IV), `verify_audit_integrity` (Art. 12), `export_audit_log`, `get_system_info` (Art. 13 transparency), `review_pending` + `get_review_status` (Art. 14 human oversight) |

Works with any MCP-compatible agent — Claude, Claude Code, OpenCode, or custom implementations. See the full [MCP Tool Reference](mcp-tools.md) for parameters and access roles.

### 🏠 Self-Hosted & GDPR-Native

Everything runs on your infrastructure as Docker containers. No external API calls required for embeddings, search (Qdrant), policies (OPA), or summarization — all can run locally via Ollama, vLLM, or TEI. Optional TLS via Caddy reverse proxy profile.

### 🔀 AI Provider Proxy

Optional gateway that sits between AI consumers and LLM providers. Injects Powerbrain tools transparently into every request, executes tool calls automatically, and returns the final response. Supports 100+ LLM providers via [LiteLLM](https://github.com/BerriAI/litellm).

Two access patterns:
1. **Direct MCP** — Agent speaks MCP natively (existing, standard)
2. **Via Proxy** — Agent speaks OpenAI-compatible API, proxy handles MCP transparently

Activate with `docker compose --profile proxy up`. OPA policies control which tools are mandatory, which providers are allowed, and iteration limits.

### ⚖️ EU AI Act Compliance Toolkit

For deployers operating high-risk AI systems, Powerbrain ships executable compliance building blocks:

- **Art. 9** — Risk register with live indicators (`GET /health` returns risk status)
- **Art. 10** — Ingestion quality gate with composite scoring and per-source thresholds
- **Art. 11/Annex IV** — Auto-generated technical documentation from live system state
- **Art. 12** — Tamper-evident SHA-256 audit hash chain with verify/export tools
- **Art. 13** — Transparency endpoint reporting models, policies, PII config, audit integrity
- **Art. 14** — Circuit breaker kill-switch + approval queue for sensitive operations
- **Art. 15** — Embedding drift detection, windowed feedback metrics, Prometheus alerts

Background maintenance via `pb-worker` (APScheduler): accuracy metrics refresh, audit retention, GDPR cleanup, review timeouts.

## Architecture Overview

```
Agent / Skill
    │ MCP (Streamable HTTP)
    ▼
┌─────────────────────────────────────────────────┐
│  MCP Server (Python, FastAPI)                   │
│  ├─ Authentication (API key + OAuth)             │
│  ├─ Rate Limiting (per-role token bucket)       │
│  ├─ OPA Policy Check (every request)            │
│  ├─ Circuit Breaker + Approval Queue (Art. 14)  │
│  ├─ Qdrant Vector Search (oversampled)          │
│  ├─ Cross-Encoder Reranking (top-k)             │
│  ├─ Context Summarization (OPA-controlled)      │
│  ├─ Sealed Vault (PII pseudonymization)         │
│  ├─ Knowledge Graph (Apache AGE/Cypher)         │
│  └─ Tamper-Evident Audit Log (Art. 12)          │
└─────────────────────────────────────────────────┘
    │           │           │           │
    ▼           ▼           ▼           ▼
 Qdrant    PostgreSQL     OPA       Ollama/vLLM/TEI
 (vectors)  (data+graph   (policies) (embeddings
             +vault+audit)            +summarization)
                │
                ▼
         ┌──────────────┐
         │  pb-worker   │  Accuracy metrics, drift
         │ (APScheduler)│  detection, audit retention
         └──────────────┘
```

**Monitoring:** Prometheus metrics, Grafana dashboards, Grafana Tempo distributed tracing (W3C traceparent propagation).

## How is This Different?

| Approach | Limitation | Powerbrain |
|----------|-----------|------------|
| **Vendor RAG APIs** (OpenAI, Pinecone) | Data leaves your infrastructure | Fully self-hosted, no external calls |
| **Vector-only search** (ChromaDB, Weaviate) | No policy layer, no PII handling | OPA policy check on every request, Sealed Vault |
| **LangChain / LlamaIndex** | Frameworks, not products; no built-in compliance | Turnkey Docker deployment with GDPR built in |
| **Enterprise search** (Elastic, Coveo) | Not MCP-native, not agent-oriented | MCP-first, designed for AI agent consumption |
| **Manual RAG pipelines** | Custom code, no standardization | Standard MCP interface, policy-as-code |
| **DIY proxy / gateway** | No MCP awareness, no tool enforcement | Transparent tool injection with policy control |

## Getting Started

- **[Quick Start](../README.md#-quick-start)** — `./scripts/quickstart.sh` for automated setup
- **[Getting Started Guide](getting-started.md)** — Step-by-step tutorial: auth, ingest, search, policies
- **[MCP Tool Reference](mcp-tools.md)** — All 23 tools with parameters and access roles
- **[Deployment Guide](deployment.md)** — Production setup, TLS, Docker Secrets
- **[Architecture](architecture.md)** — Technical deep-dive
