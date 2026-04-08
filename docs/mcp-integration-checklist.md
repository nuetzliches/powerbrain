# MCP Server Integration Checklist

Checklist for connecting a new MCP server to the pb-proxy.

## PII & Data Protection

- [ ] **Identify data sources**: which external APIs / databases does the MCP access?
- [ ] **PII classification per tool**:
  - Tools that search inside Powerbrain (`search_knowledge`) return pseudonymized data
  - Tools that call external APIs directly return raw data with potential PII
  - Mixed servers need an explicit per-tool list
- [ ] **Set `pii_status` in `mcp_servers.yaml`**:
  - `scanned` — all tool results already pseudonymized (e.g. Powerbrain MCP)
  - `unscanned` — tool results may contain PII (default, fail-safe)
  - `mixed` — per-tool declaration via `pii_scanned_tools`
- [ ] **Maintain `pii_scanned_tools`** (only for `mixed`): list of tool names (unprefixed) whose results are already pseudonymized
- [ ] **Check the ingestion path**: is data fed in via `ingest_data`? If so, the PII scanner automatically runs during ingestion (Presidio + OPA policy)
- [ ] **Inspect tool results**: do results contain person names, emails, or descriptions with PII? Declare as `unscanned`

## Authentication

- [ ] **Choose auth mode**: `bearer` (user token forwarding), `static` (fixed token from env var), `none`
- [ ] **Define `forward_headers`**: which client headers must the proxy forward to the MCP? (e.g. `X-TC-PAT` for TimeCockpit)
- [ ] **Extend OPA policy**: add the server to `mcp_servers_allowed` in `proxy.rego` (which roles may access it?)

## General

- [ ] **Set `prefix`**: unique namespace for tool names (e.g. `tc_`, `jira_`, `slack_`)
- [ ] **Set `required`**: must the server be reachable when the proxy starts? (`true` = proxy will not start without this server)
- [ ] **Check `tool_whitelist`**: should only specific tools be exposed? (default: all tools of the server)
- [ ] **Docker networking**: server must be reachable in the `proxy-net` network
- [ ] **Health check**: server should respond to tool discovery (MCP `list_tools`)

## Reference: mcp_servers.yaml example

```yaml
servers:
  - name: new-mcp
    url: http://new-mcp:3000/mcp
    auth: static
    auth_token_env: NEW_MCP_TOKEN
    prefix: new
    required: false
    pii_status: unscanned        # or: scanned, mixed
    # pii_scanned_tools:         # only for mixed
    #   - tool_that_searches_powerbrain
    forward_headers:
      - X-Custom-Auth
```
