# 🧠 Powerbrain

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
│  ├─ Qdrant Vector Search (oversampled)          │
│  ├─ Cross-Encoder Reranking (top-k)             │
│  ├─ Context Summarization (policy-controlled)   │
│  ├─ Sealed Vault (PII pseudonymization)         │
│  └─ Audit Log (GDPR-compliant)                  │
└─────────────────────────────────────────────────┘
    │           │           │           │
    ▼           ▼           ▼           ▼
 Qdrant    PostgreSQL     OPA       Ollama
 (vectors)  (data+vault)  (policies) (embeddings+LLM)
```

## ✨ Core Features

🔒 **Policy-Aware Context Delivery** — Every search request is checked against OPA policies. Classification levels (public, internal, confidential, restricted) control what each agent role can access. Compliance is executable code, not documentation.

🛡️ **Sealed Vault & Pseudonymization** — PII is detected at ingestion (Microsoft Presidio), pseudonymized with per-project salts, and stored in a dual-layer vault. Originals require HMAC-signed, time-limited tokens with purpose binding. Art. 17 deletion: remove the vault mapping and pseudonyms become irreversible.

🎯 **Relevance Pipeline** — 3-stage search: Qdrant oversampling (5x candidates) → OPA policy filtering → Cross-Encoder reranking. Graceful degradation: if the reranker is down, results fall back to vector ordering.

📝 **Context Summarization** — Agents can request summaries instead of raw chunks. OPA policies can enforce summarization for sensitive data (confidential = summary only, no raw text), control detail levels, or deny summarization entirely. Powered by Ollama.

🔌 **MCP-Native Interface** — 10 tools accessible through the Model Context Protocol. Works with any MCP-compatible agent (Claude, OpenCode, custom). One endpoint, one protocol.

🏠 **Self-Hosted & GDPR-Native** — Everything runs on your infrastructure. No external API calls for embeddings, search, or summarization. Docker Compose up and you're running.

🔀 **AI Provider Proxy** — Optional gateway between your AI consumers and their LLM providers. Transparently injects Powerbrain tools into every LLM request and executes tool calls automatically. Your teams use any LLM they prefer (100+ providers via LiteLLM); Powerbrain ensures they always query policy-checked enterprise context. Activate with `docker compose --profile proxy up`.

## 🚀 Quick Start

```bash
git clone <repo-url> && cd powerbrain
cp .env.example .env
# Edit .env: set PG_PASSWORD (and optionally FORGEJO_URL, FORGEJO_TOKEN)

docker compose up -d

# Pull the embedding model
docker exec pb-ollama ollama pull nomic-embed-text

# Create vector collections
for col in pb_general pb_code pb_rules; do
  curl -s -X PUT "http://localhost:6333/collections/$col" \
    -H 'Content-Type: application/json' \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}' && echo " → $col ✓"
done
```

Connect your agent:

```json
{
  "mcpServers": {
    "powerbrain": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

That's it. Your agent now has access to `search_knowledge`, `query_data`, `graph_query`, and 7 more tools.

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
2. Powerbrain embeds the query via Ollama (nomic-embed-text)
3. Qdrant returns 50 candidates (10 × 5 oversampling)
4. OPA filters by agent role + data classification → 30 remain
5. Cross-Encoder reranks by query-document relevance → top 10
6. OPA summarization policy: allowed? required? detail level?
7. Ollama summarizes the chunks (if applicable)
8. Response: results + summary + policy transparency
```

## 🧭 Principles

1. **Sovereignty by design** — Data sovereignty is not a feature, it's the architecture. No external API calls. No cloud dependencies. Your data, your rules.

2. **Enable, don't restrict** — The goal is not to prevent AI adoption, but to make it safely usable. Powerbrain says "yes, but with guardrails" instead of "no."

3. **Policy as code** — Compliance rules are OPA/Rego policies, version-controlled and testable. Not Word documents. Not checkbox audits.

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [What is Powerbrain?](docs/what-is-powerbrain.md) | Detailed overview and positioning |
| [Architecture](docs/architecture.md) | Technical deep-dive |
| [Deployment Guide](docs/deployment.md) | Dev, production, TLS, Docker Secrets |
| [Technology Decisions](docs/technology-decisions.md) | ADRs and trade-offs |
| [CLAUDE.md](CLAUDE.md) | Agent-facing reference (tools, schemas, conventions) |

## 🤝 Contributing

Powerbrain is open source (MIT). Contributions welcome — whether it's a new OPA policy, a better reranker model, or documentation improvements.

*Open source. Closed data.* 🔐

## 📄 License

All original code: **MIT**. Dependencies under their respective licenses.
