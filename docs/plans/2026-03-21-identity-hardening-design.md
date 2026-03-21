# Powerbrain Identity, Docs & Hardening — Design

**Date:** 2026-03-21
**Status:** Approved
**Scope:** Brand identity, context summarization, hardening, documentation

---

## 1. Brand Identity

**Tagline:** *"AI eats context. We decide what's on the menu."*

**Supporting claims:**
- *"Open source. Closed data."* — for technical contexts, badges, talks
- *"Think globally. Host locally."* — European sovereignty claim

**One-liner:**
> Open-source context engine that feeds AI agents with policy-compliant
> enterprise knowledge — self-hosted, GDPR-native, provider-agnostic.

**Principles:**
1. **Sovereignty by design** — Data sovereignty is not a feature, it's the architecture
2. **Enable, don't restrict** — The goal is not to prevent AI, but to make it safely usable
3. **Policy as code** — Compliance is executable code, not documentation

**Core features (identity-defining, to be hardened):**
1. Policy-aware Context Delivery (OPA)
2. Sealed Vault & Pseudonymization
3. Relevance Pipeline (Oversampling → Policy → Reranking)
4. Context Summarization (policy-controlled) — NEW
5. MCP-native Interface
6. Self-hosted / GDPR-native

**Non-core (useful, but not identity-defining):**
Knowledge Graph, Evaluation/Feedback, Versioning, Monitoring, Proactive Context (future)

---

## 2. Context Summarization — Architecture

### Concept

When an agent calls `search_knowledge` or `get_code_context`, it can set
`summarize=true`. OPA decides whether summarization is allowed, denied, or
enforced.

### Request flow

```
Agent → search_knowledge(query, summarize=true)
  → Qdrant (oversampled candidates)
  → OPA filter (classification + role)
  → Cross-Encoder reranking (top-k)
  → OPA summarization policy check:
      - summarize_allowed? → proceed
      - summarize_required? → force even if not requested
      - summarize_denied? → return raw chunks only
  → Ollama/LLM summarization (if applicable)
  → Response (chunks + optional summary)
```

### MCP tool changes

**New parameters for `search_knowledge` and `get_code_context`:**
- `summarize` (bool, default `false`) — agent requests a summary
- `summary_detail` (enum: `brief` | `standard` | `detailed`, default `standard`)

**Response additions:**
- `summary` (string, optional) — the generated summary
- `summary_policy` (string) — `"requested"` | `"enforced"` | `"denied"` —
  transparency for the agent

### OPA policy rules (new ruleset `kb.summarization`)

```rego
package kb.summarization

# Agent may request summaries
summarize_allowed {
    input.agent_role != "viewer"
}

# Confidential data: only summaries, never raw chunks
summarize_required {
    input.classification == "confidential"
}

# Restricted data: brief summaries only
summarize_detail := "brief" {
    input.classification == "restricted"
}
```

When `summarize_required` is true and the agent did NOT request summarization,
the system forces it and returns `summary_policy: "enforced"`. The raw chunks
are not included in the response — only the summary. This is a privacy
enhancement: the agent gets the information but never the original text.

### LLM integration

- Ollama expands from embedding-only to embedding + summarization
- New env var: `SUMMARIZATION_MODEL` (default: a small model like `qwen2.5:3b`)
- Uses Ollama `/api/generate` endpoint
- Graceful degradation: if summarization fails → return raw chunks
  (consistent with reranker fallback behavior)
- System prompt for summarization should instruct the model to preserve
  factual accuracy and not hallucinate beyond the provided chunks

---

## 3. Hardening

### 3a. Dockerfile fixes

| Service | Problem | Fix |
|---------|---------|-----|
| mcp-server | `manage_keys.py` not in image | Add `COPY manage_keys.py .` |
| mcp-server | Only `server.py` and `graph_service.py` copied | Copy all `.py` files |
| ingestion | `en_core_web_lg` not downloaded | Add `RUN python -m spacy download en_core_web_lg` |

### 3b. TLS — Caddy as optional profile

**Approach:** Docker Secrets for sensitive values + Caddy reverse proxy as
optional Docker Compose profile.

**Deployment scenarios:**

| Scenario | How |
|----------|-----|
| Dev / internal network | No TLS, direct access — default (unchanged) |
| Prod with Caddy | `docker compose --profile tls up` activates Caddy |
| Prod with own proxy | Docs: "Bring your own proxy" with upstream config |

**Docker Compose changes:**
- Add `secrets` section for `pg_password`, `hmac_key`, `api_keys`
- Add Caddy service with `profiles: [tls]`
- Caddy auto-HTTPS for `mcp-server:8080` and `ingestion:8081`
- Internal service communication remains plain HTTP (Docker network isolation)

**Docker Secrets migration:**
- `.env` keeps non-sensitive values (ports, model names, feature flags)
- Sensitive values move to Docker Secrets (`pg_password`, `hmac_secret`,
  `forgejo_token`)
- Services read from `/run/secrets/<name>` with `.env` fallback for
  backward compatibility

### 3c. Documentation for proxy scenarios

New doc: `docs/deployment.md` covering:
- Dev setup (no TLS)
- Prod with Caddy profile
- Prod with external proxy (Nginx/Traefik/Caddy example configs)
- Docker Secrets setup

---

## 4. Documentation updates

### 4a. README.md — full rewrite

**Tone:** Casual with personality (openclaw-style). Emojis welcome.
**Language:** English.

**Structure:**
1. Logo + tagline (*"AI eats context. We decide what's on the menu."*)
2. One-liner: what Powerbrain is
3. **The Problem** — 2-3 sentences, pointed
4. **The Solution** — simplified architecture diagram
5. **Core Features** — the 6 core features, emoji + 1-2 sentences each
6. **Quick Start** — `docker compose up -d` → done in 5 minutes
7. **How It Works** — simplified pipeline flow diagram
8. **Principles** — the 3 principles
9. **Documentation** — links to docs/
10. **Contributing** — community invitation
11. **License**

### 4b. `docs/what-is-powerbrain.md` — rewrite

- English, aligned with new identity
- Target: someone seeing the project for the first time
- Sections: Problem, Solution, Core Features, Architecture Overview,
  "How is this different from X?"

### 4c. `CLAUDE.md` — update

- Update project description to "context engine" positioning
- Add Summarization to MCP tools list
- Add Caddy (optional) to components table
- Add `SUMMARIZATION_MODEL` to key decisions
- Keep technical tone (agent documentation, not user-facing)

---

## 5. Implementation order

1. **Dockerfile fixes** — small, immediate, no risk
2. **OPA summarization policies** — new rego ruleset
3. **MCP server summarization** — new parameters, Ollama integration
4. **Docker Secrets migration** — `.env` → secrets for sensitive values
5. **Caddy profile** — optional TLS profile in docker-compose.yml
6. **README.md rewrite** — new identity, casual tone
7. **what-is-powerbrain.md rewrite** — full English rewrite
8. **CLAUDE.md update** — reflect all changes
9. **docs/deployment.md** — new deployment guide

---

## 6. Out of scope (for now)

- CI/CD pipeline
- Ingestion adapters (CSV/JSON/Git)
- Forgejo bundle-polling activation
- Proactive context / subscription model
- Knowledge Graph hardening
