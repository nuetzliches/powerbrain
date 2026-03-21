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
                    Qdrant · PostgreSQL · OPA · Ollama
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

Powered by Ollama with configurable models. Falls back to raw chunks if summarization fails.

### 🔌 MCP-Native Interface

10 tools accessible through a single MCP endpoint:

| Tool | Purpose |
|------|---------|
| `search_knowledge` | Semantic search with optional summarization |
| `get_code_context` | Code-specific search with reranking |
| `query_data` | Structured PostgreSQL queries |
| `graph_query` | Knowledge graph traversal (Apache AGE) |
| `graph_mutate` | Graph modifications (developer/admin only) |
| `check_policy` | OPA policy evaluation |
| `get_rules` | Business rules for a context |
| `ingest_data` | Data ingestion |
| `get_classification` | Data classification lookup |
| `list_datasets` | Available datasets |

Works with any MCP-compatible agent — Claude, OpenCode, or custom implementations.

### 🏠 Self-Hosted & GDPR-Native

Everything runs on your infrastructure as Docker containers. No external API calls for embeddings (Ollama), search (Qdrant), policies (OPA), or summarization (Ollama). Optional TLS via Caddy reverse proxy profile.

### 🔀 AI Provider Proxy

Optional gateway that sits between AI consumers and LLM providers. Injects Powerbrain tools transparently into every request, executes tool calls automatically, and returns the final response. Supports 100+ LLM providers via [LiteLLM](https://github.com/BerriAI/litellm).

Two access patterns:
1. **Direct MCP** — Agent speaks MCP natively (existing, standard)
2. **Via Proxy** — Agent speaks OpenAI-compatible API, proxy handles MCP transparently

Activate with `docker compose --profile proxy up`. OPA policies control which tools are mandatory, which providers are allowed, and iteration limits.

## Architecture Overview

```
Agent / Skill
    │ MCP (Streamable HTTP)
    ▼
┌─────────────────────────────────────────────────┐
│  MCP Server (Python, FastAPI)                   │
│  ├─ Authentication (API key verification)       │
│  ├─ Rate Limiting (per-role token bucket)       │
│  ├─ OPA Policy Check (every request)            │
│  ├─ Qdrant Vector Search (oversampled)          │
│  ├─ Cross-Encoder Reranking (top-k)             │
│  ├─ Context Summarization (OPA-controlled)      │
│  ├─ Sealed Vault (PII resolution)               │
│  ├─ Knowledge Graph (Apache AGE/Cypher)         │
│  └─ Audit Log (GDPR-compliant)                  │
└─────────────────────────────────────────────────┘
    │           │           │           │
    ▼           ▼           ▼           ▼
 Qdrant    PostgreSQL     OPA       Ollama
 (vectors)  (data+graph   (policies) (embeddings
             +vault+audit)            +summarization)
```

**Monitoring:** Prometheus metrics, Grafana dashboards, Grafana Tempo distributed tracing.

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

See the [README](../README.md) for quick start instructions and [Deployment Guide](deployment.md) for production setup. The full technical reference is in [CLAUDE.md](../CLAUDE.md).
