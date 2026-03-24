# Design: Proxy Authentication + Multi-MCP-Server-Aggregation

**Status:** Approved  
**Date:** 2026-03-24  
**Scope:** pb-proxy authentication, identity propagation, multi-MCP-server tool aggregation

## Problem

The proxy has no authentication — every request gets hardcoded `developer` rights. All MCP
tool calls go through a single static admin token (`kb_dev_localonly_do_not_use_in_production`),
losing the real user's identity. Additionally, the proxy can only connect to a single MCP server.

## Design Decisions

| Decision | Chosen | Alternatives Considered | Reason |
|----------|--------|------------------------|--------|
| Auth mechanism | Shared `api_keys` table | Separate proxy keys, Bearer passthrough | One key for everything, no duplication |
| LLM provider keys | Central config only (env vars) | User-supplied via header, composite token | Simplifies auth model, no dual-header complexity |
| Identity propagation | Forward user's `kb_` key to MCP servers | Service-to-service token, impersonation header | Defense-in-depth, MCP server verifies independently |
| MCP server config | YAML file (`mcp_servers.yaml`) | Env var list, dynamic registration API | Rich per-server config (auth mode, prefix, whitelist) |
| Tool namespacing | Always prefix: `{prefix}_{tool}` | First-wins, prefix only on conflict | Unambiguous, predictable |
| Availability mode | `required` flag per server | Global setting | GDPR fail-fast for policy-critical servers, graceful degradation for optional ones |

## 1. Proxy Authentication

### Architecture

```
Client                    Proxy                     MCP-Server(s)
  │                         │                           │
  │ Bearer: kb_abc123       │                           │
  ├────────────────────────►│                           │
  │                    verify key                       │
  │                    (SHA-256 lookup                  │
  │                     in api_keys)                    │
  │                         │                           │
  │                    agent_id = "team-ci"             │
  │                    agent_role = "developer"         │
  │                         │                           │
  │                    OPA check                        │
  │                    (kb.proxy)                       │
  │                         │  Bearer: kb_abc123        │
  │                         ├──────────────────────────►│
  │                         │  (MCP-Server verifies     │
  │                         │   same key, same identity)│
  │                         │◄──────────────────────────┤
  │◄────────────────────────│                           │
```

### Key Verification

The proxy connects to PostgreSQL directly (read-only on `api_keys` table). On each request:

1. Extract Bearer token from `Authorization` header
2. SHA-256 hash the token
3. Look up `key_hash` in `api_keys` where `active = true` and `expires_at` is null or in future
4. Update `last_used_at`
5. Return `agent_id` and `agent_role`

Key lookup results are cached with a short TTL (60 seconds) to reduce DB load. Cache is
invalidated on 401 responses from downstream MCP servers.

### Identity Propagation

The user's `kb_` key is forwarded as-is to MCP servers configured with `auth: bearer`. The MCP
server verifies the same key independently (defense-in-depth). This means:

- Audit logs at the MCP server show the real user, not a service account
- OPA policies at the MCP server apply to the real user's role
- Vault access tokens are bound to the real user's identity
- Rate limiting at the MCP server applies per-user

### LLM Provider Keys

LLM provider keys come exclusively from central configuration:

- `litellm_config.yaml` — for alias-mode models
- Environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) — for passthrough-mode models

No user-supplied LLM keys. The Bearer header is always a Powerbrain `kb_` key (or absent).

### AUTH_REQUIRED Toggle

`AUTH_REQUIRED` environment variable (default: `true`).

- `true` — Reject unauthenticated requests with 401
- `false` — Fall back to hardcoded `developer` role (legacy/dev mode)

### Key Management

No key management API in the proxy. Keys are managed via:

- `manage_keys.py` CLI (create, list, revoke)
- Direct PostgreSQL access

The proxy is a read-only consumer of the `api_keys` table.

## 2. Multi-MCP-Server Aggregation

### Configuration

New file `pb-proxy/mcp_servers.yaml`:

```yaml
servers:
  - name: powerbrain
    url: http://mcp-server:8080/mcp
    auth: bearer             # forwards user's kb_ key
    prefix: powerbrain       # tool prefix: powerbrain_search_knowledge
    required: true           # fail-fast if unreachable

  - name: github
    url: http://github-mcp:3000/mcp
    auth: static             # static token from env var
    auth_token_env: GITHUB_MCP_TOKEN
    prefix: github           # github_list_repos
    required: false          # graceful degradation

  - name: internal-tools
    url: http://toolserver:8080/mcp
    auth: none               # no auth (internal network)
    prefix: tools
    required: false
    tool_whitelist:           # optional: only expose these tools
      - run_query
      - get_status
```

### Auth Modes Per Server

| Mode | Behavior |
|------|----------|
| `bearer` | Forwards the user's `kb_` key as Bearer token |
| `static` | Uses a fixed token from the env var specified in `auth_token_env` |
| `none` | No Authorization header |

### Tool Aggregation

```
ToolInjector
  ├─ ServerConnection("powerbrain", url, auth=bearer)
  │   ├─ search_knowledge → powerbrain_search_knowledge
  │   ├─ query_data       → powerbrain_query_data
  │   └─ ...
  ├─ ServerConnection("github", url, auth=static)
  │   ├─ list_repos      → github_list_repos
  │   └─ search_code     → github_search_code
  └─ ServerConnection("internal-tools", url, auth=none)
      ├─ run_query       → tools_run_query
      └─ get_status      → tools_get_status
```

Every tool gets a prefix: `{prefix}_{original_name}`. The LLM sees prefixed names. When a
tool call comes back, the proxy strips the prefix to determine the server and original tool name.

### Tool Discovery

On startup and periodically (every `TOOL_REFRESH_INTERVAL` seconds, default 60):

1. Connect to each configured server via Streamable HTTP
2. Call `list_tools()`
3. Apply `tool_whitelist` filter if configured
4. Add prefix to each tool name
5. Convert to OpenAI function-calling format
6. Cache results

If a non-required server is unreachable, its tools are removed from the cache. If a required
server is unreachable at startup, the proxy refuses to start. If a required server becomes
unreachable at runtime, the proxy returns 503 for requests that would need that server's tools.

### Tool Routing

The `_mcp_tools` dict changes from `dict[str, Any]` to `dict[str, ToolEntry]`:

```python
@dataclass
class ToolEntry:
    server_name: str       # e.g., "powerbrain"
    original_name: str     # e.g., "search_knowledge"
    schema: dict           # OpenAI function-calling schema
    auth_mode: str         # "bearer", "static", "none"
```

When the LLM returns a tool call for `powerbrain_search_knowledge`:

1. Look up `ToolEntry` by prefixed name
2. Get `server_name` → find `ServerConnection`
3. Build auth headers based on `auth_mode`
4. Call `original_name` on that server's MCP endpoint
5. Return result to agent loop

## 3. OPA Policy Extensions

### New Rule: `mcp_servers_allowed`

```rego
# Default: only powerbrain
default mcp_servers_allowed := ["powerbrain"]

# Developer and admin: all configured servers
mcp_servers_allowed := input.configured_servers if {
    input.agent_role == "developer"
}
mcp_servers_allowed := input.configured_servers if {
    input.agent_role == "admin"
}
```

The proxy sends `configured_servers` (list of all server names from config) as input.
OPA returns which ones the agent may use. Tools from disallowed servers are filtered out
before injection into the LLM request.

## 4. Error Handling

| Error | Response |
|-------|----------|
| Invalid/expired key | 401 Unauthorized |
| Valid key, `provider_allowed=false` | 403 Forbidden |
| Required MCP server unreachable | 503 Service Unavailable |
| Optional MCP server unreachable | Tools from that server unavailable, request continues |
| Tool call on unreachable server | Tool result: `{"error": "Server unavailable"}` — LLM can react |
| DB unreachable (key verification) | 503 Service Unavailable |

## 5. Request Flow (Complete)

```
1. POST /v1/chat/completions with Bearer: kb_xxx
2. Key verification → agent_id, agent_role (or 401)
3. OPA: kb.proxy → provider_allowed, mcp_servers_allowed, max_iterations, ... (or 403)
4. PII protection (unchanged)
5. Tool injection: all tools from allowed servers, with prefix
6. LLM call (alias or central provider keys)
7. Agent loop:
   a. Call LLM with merged tools
   b. If tool_calls in response:
      - De-pseudonymize tool arguments (PII reverse map)
      - Strip prefix → resolve server + original tool name
      - Build auth headers for target server
      - Execute via Streamable HTTP
      - Append tool results to messages
      - Re-call LLM
   c. Repeat until no tool_calls or max_iterations
8. De-pseudonymize response text
9. Return OpenAI-compatible response (or SSE stream)
```

## 6. Affected Files

| File | Changes |
|------|---------|
| `pb-proxy/proxy.py` | Auth middleware, identity extraction, pass user key to tool calls |
| `pb-proxy/config.py` | `AUTH_REQUIRED`, `PG_*` config, `MCP_SERVERS_CONFIG` path |
| `pb-proxy/auth.py` | **New**: `ProxyKeyVerifier` — DB lookup, caching, key validation |
| `pb-proxy/tool_injection.py` | Multi-server aggregation, prefix management, server routing |
| `pb-proxy/agent_loop.py` | Server-aware tool routing, per-server auth headers |
| `pb-proxy/mcp_servers.yaml` | **New**: MCP server definitions |
| `opa-policies/kb/proxy.rego` | `mcp_servers_allowed` rule |
| `opa-policies/kb/test_proxy.rego` | Tests for new rule |
| `docker-compose.yml` | DB connection for proxy, `AUTH_REQUIRED` env var |

## 7. Scope Exclusions

- Key management API in proxy (stays with `manage_keys.py`)
- Granular scopes / Phase 2 (deferred, documented in api-key-auth-design.md)
- Connection pooling for MCP sessions (backlog)
- Client tool passthrough (backlog)
- User-supplied LLM provider keys (not needed)

## 8. Backward Compatibility

- `AUTH_REQUIRED=false` preserves current behavior (hardcoded `developer`)
- Existing `MCP_SERVER_URL` env var is read as fallback if `mcp_servers.yaml` is absent
  (single-server mode with default prefix `powerbrain`)
- LLM provider key resolution from env vars is unchanged
- OPA `kb.proxy` policy remains backward-compatible (new rules have defaults)
