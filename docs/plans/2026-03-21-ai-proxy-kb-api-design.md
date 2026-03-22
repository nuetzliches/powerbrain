# AI Provider Proxy & Knowledge Base API — Design

**Date:** 2026-03-21
**Status:** Approved
**Scope:** AI Provider Proxy (implement), KB REST API (backlog), Identity update

---

## 1. Summary

Two new capabilities for Powerbrain:

1. **AI Provider Proxy** ("Context Gateway") — optional service that sits between
   AI consumers and LLM providers, transparently injecting Powerbrain tools into
   every request and executing tool calls. Ensures agents always use enterprise
   context regardless of LLM provider. **Implement now.**

2. **Knowledge Base REST API** — separate service exposing the existing search
   pipeline via REST for human users (web UI, internal portals). **Backlog.**

Both features integrate into the existing Powerbrain identity as extensions of
the "context engine" positioning.

---

## 2. Research Findings

### AI Provider Proxy — OS Landscape

No existing open-source solution provides transparent MCP tool injection:

| Solution | LLM Routing | MCP Support | Tool Injection | Gap |
|----------|-------------|-------------|----------------|-----|
| **LiteLLM** (40k stars, MIT) | 100+ providers | Native MCP Gateway | Client must opt-in | No transparent injection |
| **Portkey Gateway** (11k stars, MIT) | 200+ providers | Separate MCP Gateway | No | LLM + MCP are separate |
| **Open WebUI** (128k stars) | Ollama + OpenAI | Limited | Via plugins, UI-bound | Not a proxy |

**Decision:** Use LiteLLM as a Python dependency for provider routing. Build the
tool injection and execution loop as a custom FastAPI service on top.

**LiteLLM License:** MIT (core), enterprise/ directory under commercial license.
Only the MIT-licensed core is used as a dependency.

### Knowledge Base for Users — OS Landscape

No existing solution fits the Powerbrain stack:

| Solution | Qdrant | OPA | Auth | Status | Fit |
|----------|--------|-----|------|--------|-----|
| **Onyx/Danswer** (18k stars) | No (Vespa) | No | SSO/RBAC | Active | Poor — parallel system |
| **PrivateGPT** (57k stars) | Yes (default) | No | Minimal | Stalled | Moderate — no auth |
| **Quivr** (39k stars) | Via LlamaIndex | No | None | Slowed | Poor — library only |
| **Outline** (38k stars) | No | No | SSO/RBAC | Active | Wrong category (wiki) |

**Decision:** Build a REST API service on the existing stack when needed. The
existing search pipeline, OPA policies, reranker, and Sealed Vault are 90%
reusable. Estimated effort: 2-4 weeks.

---

## 3. AI Provider Proxy — Architecture

### 3a. Positioning

- **Optional layer**, activated via Docker Compose profile (`proxy`)
- Consistent with Caddy TLS pattern (`docker compose --profile proxy up`)
- Powerbrain remains primarily a "context engine"
- The proxy is an addon that enforces context usage

### 3b. Architecture diagram

```
Client (Agent / App / IDE)
    │  OpenAI-compatible API
    │  POST /v1/chat/completions
    ▼
┌──────────────────────────────────────────────────┐
│  pb-proxy (FastAPI + LiteLLM)                    │
│  profiles: [proxy]                               │
│                                                  │
│  ┌─ Request Pipeline ──────────────────────────┐ │
│  │  1. Auth (API key → agent_role)             │ │
│  │  2. OPA: kb.proxy.provider_allowed?         │ │
│  │  3. Tool Injection:                         │ │
│  │     → Merge Powerbrain tools into tools[]   │ │
│  │  4. Forward to LLM (via LiteLLM)           │ │
│  └─────────────────────────────────────────────┘ │
│                                                  │
│  ┌─ Agent Loop ────────────────────────────────┐ │
│  │  while response has tool_calls:             │ │
│  │    → Execute tool via MCP client            │ │
│  │    → Append result to messages              │ │
│  │    → Re-call LLM with updated messages      │ │
│  │  return final response to client            │ │
│  └─────────────────────────────────────────────┘ │
│                                                  │
│  Observability: Prometheus + OTel tracing        │
│  Audit: every tool execution logged              │
└──────────────────────────────────────────────────┘
    │                         │
    ▼                         ▼
  LLM Provider             Powerbrain MCP Server
  (via LiteLLM)            (existing, port 8080)
```

### 3c. Components

**Service: `pb-proxy/`**

```
pb-proxy/
├── proxy.py           ← Main FastAPI application
├── tool_injection.py  ← Tool discovery + merge logic
├── agent_loop.py      ← Tool-call execution loop
├── config.py          ← LiteLLM + MCP configuration
├── Dockerfile
└── requirements.txt   ← litellm, mcp, fastapi, httpx
```

**Port:** 8090 (avoids conflict with mcp-server:8080, ingestion:8081,
reranker:8082)

### 3d. Request flow (detailed)

```
1. Client sends POST /v1/chat/completions
   {model: "gpt-4o", messages: [...], tools: [...client_tools]}

2. Auth middleware verifies API key → agent_id, agent_role

3. OPA check: kb.proxy.provider_allowed
   Input: {agent_role, provider: "gpt-4o", action: "chat"}
   → Denied? Return 403

4. Tool injection:
   a. Load cached Powerbrain tool definitions (from MCP list_tools)
   b. Convert MCP Tool → OpenAI function schema
   c. Merge into request.tools[] (Powerbrain tools take precedence
      over client tools with same name)

5. Forward augmented request to LLM via litellm.acompletion()

6. Agent loop:
   a. LLM responds with tool_calls? → execute each:
      - Powerbrain tool → mcp_client.call_tool(name, args)
      - Unknown tool → return error result to LLM
   b. Append tool results to messages
   c. Re-call LLM (step 5)
   d. Repeat until: final response OR max_iterations reached

7. Return final response to client
   (standard OpenAI response format)

8. Audit log: agent_id, provider, tools_used, iterations, latency
```

### 3e. Tool injection details

**Discovery:** On startup and every 60 seconds, the proxy calls the Powerbrain
MCP server's `list_tools()` to get current tool definitions. Results are cached
in memory.

**Schema conversion:** MCP `Tool` objects use JSON Schema for `inputSchema`.
OpenAI function-calling uses a compatible but slightly different format. The
conversion is straightforward:

```python
def mcp_tool_to_openai(tool: MCPTool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.inputSchema,
        }
    }
```

**Merge strategy:**
- Powerbrain tools are always included (transparent injection)
- If client sends a tool with the same name as a Powerbrain tool,
  the Powerbrain version wins (prevents circumvention)
- Client tools with unique names are preserved (passthrough)

### 3f. Agent loop

**Max iterations:** Configurable via OPA policy per agent role (default: 10).
Prevents runaway loops.

**Timeout:** Per tool-call timeout (default: 30s). Total request timeout
(default: 120s).

**Error handling:**
- Tool execution fails → return error as tool result to LLM
  (LLM can decide how to proceed)
- Max iterations reached → return last LLM response with warning header
- MCP server unreachable → fail open (forward request without injection)
  or fail closed (return 503) — configurable

**Streaming:** Supported via simulated SSE streaming — the proxy runs
the agent loop to completion, then streams the response as SSE chunks.
Model discovery via `GET /v1/models` returns all configured models.

### 3g. OPA policies

New Rego package `kb.proxy`:

```rego
package kb.proxy

import future.keywords.in

# Which tools must be injected into every request
default required_tools := {"search_knowledge", "check_policy"}

# Agent role may use the proxy
provider_allowed {
    input.agent_role in {"analyst", "developer", "admin"}
}

# Max agent-loop iterations per role
max_iterations := 5 {
    input.agent_role == "analyst"
}
max_iterations := 10 {
    input.agent_role in {"developer", "admin"}
}

# Provider restrictions (optional — all allowed by default)
provider_denied {
    input.provider == "gpt-4o"
    input.agent_role == "viewer"
}
```

### 3h. Configuration

**LiteLLM configuration** (`litellm_config.yaml`):

```yaml
model_list:
  - model_name: "gpt-4o"
    litellm_params:
      model: "openai/gpt-4o"
      api_key: "os.environ/OPENAI_API_KEY"
  - model_name: "claude-sonnet"
    litellm_params:
      model: "anthropic/claude-sonnet-4-20250514"
      api_key: "os.environ/ANTHROPIC_API_KEY"
  - model_name: "local-llama"
    litellm_params:
      model: "ollama/llama3.2"
      api_base: "http://ollama:11434"
```

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROXY_PORT` | `8090` | Service port |
| `MCP_SERVER_URL` | `http://mcp-server:8080/mcp` | Powerbrain MCP endpoint |
| `LITELLM_CONFIG` | `/app/litellm_config.yaml` | LiteLLM model configuration |
| `TOOL_REFRESH_INTERVAL` | `60` | Seconds between tool list refresh |
| `MAX_ITERATIONS` | `10` | Default max agent-loop iterations |
| `TOOL_CALL_TIMEOUT` | `30` | Timeout per tool call (seconds) |
| `REQUEST_TIMEOUT` | `120` | Total request timeout (seconds) |
| `FAIL_MODE` | `closed` | `open` or `closed` when MCP is unreachable |
| `OPA_URL` | `http://opa:8181` | OPA endpoint |

### 3i. Docker Compose integration

```yaml
pb-proxy:
  build: ./pb-proxy
  container_name: kb-proxy
  profiles: [proxy]
  ports:
    - "${PROXY_PORT:-8090}:8090"
  environment:
    - MCP_SERVER_URL=http://mcp-server:8080/mcp
    - OPA_URL=http://opa:8181
    - LITELLM_CONFIG=/app/litellm_config.yaml
    - TOOL_REFRESH_INTERVAL=${TOOL_REFRESH_INTERVAL:-60}
    - MAX_ITERATIONS=${MAX_ITERATIONS:-10}
    - FAIL_MODE=${FAIL_MODE:-closed}
  volumes:
    - ./pb-proxy/litellm_config.yaml:/app/litellm_config.yaml:ro
  depends_on:
    mcp-server:
      condition: service_healthy
    opa:
      condition: service_healthy
  networks:
    - kb-net
  restart: unless-stopped
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8090/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

Activation: `docker compose --profile proxy up -d`

---

## 4. Identity Update

### 4a. New core feature (7th)

The proxy becomes the 7th identity-defining core feature:

**Current 6:**
1. Policy-aware Context Delivery (OPA)
2. Sealed Vault & Pseudonymization
3. Relevance Pipeline (Oversampling → Policy → Reranking)
4. Context Summarization (policy-controlled)
5. MCP-native Interface
6. Self-hosted / GDPR-native

**New 7th:**
7. AI Provider Proxy (transparent tool enforcement)

### 4b. Updated one-liner

> Open-source context engine that feeds AI agents with policy-compliant
> enterprise knowledge — self-hosted, GDPR-native, provider-agnostic.
> Optional proxy ensures agents always use enterprise context, regardless
> of which LLM they talk to.

### 4c. New supporting claim

*"Bring your own LLM. Keep our guardrails."* — for proxy marketing contexts

### 4d. Feature description (for README/docs)

> **AI Provider Proxy** — Optional gateway that sits between your AI consumers
> and their LLM providers. Transparently injects Powerbrain tools into every
> LLM request and executes tool calls automatically. Your teams use whichever
> LLM they prefer (100+ providers via LiteLLM); Powerbrain ensures they always
> query policy-checked enterprise context. Activate with
> `docker compose --profile proxy up`.

### 4e. Principle alignment

The proxy reinforces all three existing principles:
- **Sovereignty by design** — Enterprise controls what context the LLM sees
- **Enable, don't restrict** — Teams choose their LLM, guardrails are transparent
- **Policy as code** — OPA decides which tools are mandatory, which providers
  are allowed, and how many iterations are permitted

### 4f. Updated architecture diagram (for README)

```
Agent / Skill
    │ OpenAI API (optional proxy)      │ MCP (direct)
    ▼                                  ▼
┌──────────────┐              ┌─────────────────────┐
│  pb-proxy    │──────MCP────▶│  Powerbrain         │
│  (optional)  │              │  MCP Server          │
│  LiteLLM     │              │  ├─ OPA Policy       │
│  Tool inject │              │  ├─ Qdrant Search    │
│  Agent loop  │              │  ├─ Reranker         │
└──────┬───────┘              │  ├─ Summarization    │
       │                      │  ├─ Sealed Vault     │
       ▼                      │  └─ Audit Log        │
  LLM Provider               └─────────────────────┘
  (100+ via LiteLLM)              │       │       │
                                  ▼       ▼       ▼
                              Qdrant  PostgreSQL  OPA
```

Two access patterns:
1. **Direct MCP** — Agent speaks MCP natively (existing, unchanged)
2. **Via Proxy** — Agent speaks OpenAI API, proxy handles MCP transparently

---

## 5. Knowledge Base REST API — Backlog Spec

Deferred to a future sprint. When implemented:

### 5a. Architecture

Separate FastAPI service (`kb-api/`), optional Docker Compose profile (`api`).
Reuses same backends (Qdrant, OPA, PostgreSQL, Reranker, Ollama).

### 5b. Endpoints

```
POST /api/v1/search          # Semantic search (same pipeline as MCP)
POST /api/v1/ask             # Q&A (search + summarization)
GET  /api/v1/documents/{id}  # Document view
GET  /api/v1/documents       # Browse with pagination + filters
GET  /api/v1/collections     # List collections
GET  /api/v1/graph/explore   # Knowledge graph browser
POST /api/v1/auth/login      # OIDC/SSO login
GET  /api/v1/auth/me         # Current user + permissions
```

### 5c. Prerequisites

- Extract search logic from `_dispatch()` into shared `core/` service layer
- Add OIDC/JWT authentication (separate from API key auth)
- Add CORS middleware, pagination, response formatting
- Optional: embedding cache, OPA batch evaluation for multi-user load

### 5d. Estimated effort

2-4 weeks, with ~50% of the time on the service layer extraction (which also
benefits the MCP server and proxy).

---

## 6. Backlog Items

| Item | Priority | Dependency |
|------|----------|------------|
| Multi-MCP-Server support for proxy | Medium | Proxy MVP |
| ~~SSE streaming through proxy~~ | ~~Medium~~ | ✅ Implemented |
| Model discovery (wildcard/passthrough routing) | Medium | Proxy MVP |
| Proxy authentication (separate from provider keys) | Medium | Proxy MVP |
| Client tool passthrough | Low | Proxy MVP |
| KB REST API service | Medium | Service layer extraction |
| KB Web UI (React/Next.js) | Low | KB REST API |

---

## 7. Implementation Order

1. **OPA proxy policies** — `opa-policies/kb/proxy.rego`
2. **pb-proxy service** — FastAPI + LiteLLM + MCP client
3. **Docker Compose profile** — `profiles: [proxy]`
4. **Identity updates** — README, what-is-powerbrain, CLAUDE.md
5. **Deployment docs** — proxy section in deployment.md
6. **Tests** — proxy unit tests + integration tests

---

## 8. Out of Scope

- Multi-MCP-Server aggregation (backlog)
- ~~SSE streaming passthrough~~ (✅ implemented)
- KB REST API (backlog)
- Web UI for knowledge base (backlog)
- Custom LiteLLM plugins (use SDK directly)
- OAuth2 for proxy consumers (API key auth is sufficient for MVP)
