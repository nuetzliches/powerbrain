# Editions — Community vs Enterprise

Powerbrain ships as two tiers, both Apache-2.0 licensed and fully
self-hosted. The split exists because they answer different operational
questions:

* **Community** is the context engine — data ingestion, classification,
  policy enforcement, vault storage, MCP tools. Everything an agent
  framework needs to pull compliant context into its own reasoning loop.
* **Enterprise** is the chat-native gateway around that engine — tool
  injection, agent-loop orchestration, PII protection on the wire, and
  purpose-bound vault resolution for tool results. Enterprise is what
  turns the community data layer into a working chat UX without custom
  integration code.

## Edition detection

Every core service advertises its edition on `/health` and
`/transparency`:

```
$ curl -sH 'Accept: application/json' http://localhost:8080/health | jq .edition
"community"

$ curl -s http://localhost:8090/health | jq .edition
"enterprise"
```

A deployment running `docker compose --profile proxy` (or `--profile
demo`, which includes `proxy`) is the enterprise tier. A deployment
running only the base profiles is community.

## Capability matrix

| Capability | Community (MCP only) | Enterprise (+ pb-proxy) |
|---|---|---|
| Vector search + reranking (`search_knowledge`) | ✅ | ✅ |
| Knowledge graph (`graph_query`, `graph_mutate`) | ✅ | ✅ |
| Ingestion pipeline (PII scan, quality gate, vault) | ✅ | ✅ |
| OPA policy enforcement on every request | ✅ | ✅ |
| Sealed vault (HMAC-token reveal on `search_knowledge`) | ✅ | ✅ |
| EU AI Act Art. 9–15 surfaces (audit chain, transparency, oversight) | ✅ | ✅ |
| Structured metrics + OTel traces | ✅ | ✅ |
| OpenAI-compatible `/v1/chat/completions` endpoint | — | ✅ |
| Agent loop — automatic tool-call execution against MCP | — | ✅ |
| 100+ LLM providers via LiteLLM (central/user/hybrid key modes) | — | ✅ |
| Chat-path PII pseudonymisation (request + response) | — | ✅ |
| Vault resolution for tool-call pseudonyms (`/vault/resolve`) | — | ✅ |
| Per-provider key management + `X-Provider-Key` header | — | ✅ |
| Chat document attachments (PDF/DOCX/XLSX extraction via OPA) | — | ✅ |

## Which should I run?

**Community fits when:**

- You already have an agent framework (Claude Desktop, custom
  LangGraph/LlamaIndex, OpenCode, your own orchestrator) and want a
  drop-in compliant context layer behind it.
- You treat pseudonyms in the retrieved chunks as acceptable agent
  context (LLM reasons on `[PERSON:xxx]` tokens and surfaces only what
  the user's prompt provides).
- You want to mint vault tokens yourself per request and handle
  post-processing in your own code.

**Enterprise fits when:**

- You want users to chat directly with the system and see finished
  answers with the right PII resolved, without writing any orchestration
  code.
- You need centralised per-call policy on *which* purposes and *which*
  roles may trigger vault resolution — without every agent owning the
  vault HMAC secret.
- You want one endpoint that multiplexes dozens of LLM providers
  (Anthropic, OpenAI, Azure, Bedrock, GitHub Models, local Ollama…)
  behind a stable OpenAI-compatible surface.
- You want chat-side document attachments (OPA-gated, MIME-allow-listed)
  processed through the same extraction pipeline the ingestion adapters
  use.

Both tiers share the same policy data, same audit chain, same vault.
Moving from community to enterprise is a `docker compose --profile
proxy up -d` — no data migration.

## Deployment topology

```
                        ┌───────────────────────────────────┐
                        │  Agent frameworks / Claude Desktop │
                        │     (community: direct to MCP)     │
                        └────────────────┬──────────────────┘
                                         │ MCP (HTTP+JSON-RPC)
┌─────────────────────┐                  │
│  Chat clients       │  OpenAI-compat   │
│  (enterprise only)  │─────────────────▶│
└─────────────────────┘                  │
              │                          │
              ▼                          │
     ┌─────────────────┐                 │
     │  pb-proxy :8090 │────────tool-call routing────┐
     │  (enterprise)   │                             │
     │  - /v1/chat     │────────/vault/resolve───────┤
     │  - PII wire     │                             ▼
     │  - agent loop   │                    ┌───────────────────┐
     │  - LiteLLM      │                    │  mcp-server :8080 │
     └─────────────────┘                    │  (community)      │
                                            │  - search         │
                                            │  - vault          │
                                            │  - graph          │
                                            │  - OPA            │
                                            └───────────────────┘
```

In the enterprise topology, clients only ever see pb-proxy. The proxy
still forwards the user's API key to mcp-server so the OPA identity
decisions match what a direct-to-MCP agent would see — same audit, same
role-based filtering.

## Configuring the edition

### Community (default)

```
docker compose --profile local-llm --profile local-reranker up -d
```

No `pb-proxy` container. Agents call `http://localhost:8080/mcp`
directly with `Authorization: Bearer pb_…`.

### Enterprise

```
docker compose --profile local-llm --profile local-reranker --profile proxy up -d
```

Clients now use `http://localhost:8090/v1/chat/completions`. The proxy
reads `pb-proxy/litellm_config.yaml` for model aliases, `mcp_servers.yaml`
for which MCP endpoints to injector, and OPA for policy
(`pb.proxy.pii_resolve_tool_results`).

### Sales demo (both tiers side-by-side)

```
./scripts/quickstart.sh --demo
```

Pulls the extra Ollama model (qwen2.5:3b), brings up all profiles, and
opens http://localhost:8095 Tab D to compare MCP and pb-proxy responses
on the same query.

## Policy surfaces unique to enterprise

`opa-policies/data.json → pb.config.proxy`:

```json
"proxy": {
  "pii_resolve_tool_results": {
    "enabled": true,
    "allowed_roles": ["analyst", "developer", "admin"],
    "allowed_purposes": ["support", "billing", "contract_fulfillment",
                         "hr_management", "payroll"],
    "default_purpose": "support"
  }
}
```

When a chat request reaches pb-proxy:

1. The client may set `X-Purpose: billing` (OpenAI-compatible extension).
2. `pb.proxy.pii_resolve_tool_results_allowed` evaluates
   (role, purpose). Denied → the proxy behaves like community
   (pseudonyms flow straight through to the LLM).
3. Allowed → every tool-result chunk containing `[TYPE:hash]` tokens is
   sent to mcp-server `POST /vault/resolve`, which applies
   `pb.privacy.vault_access_allowed` + `vault_fields_to_redact`
   per document's classification and data_category.
4. Every successful resolution adds a `pii_vault.vault_access_log` row;
   the audit chain proves the access happened.

Both policy rules are editable at runtime via the `manage_policies` MCP
tool (admin only).
