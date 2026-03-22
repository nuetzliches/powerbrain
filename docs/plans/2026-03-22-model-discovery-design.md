# Model Discovery / Wildcard Routing — Design

**Date:** 2026-03-22
**Status:** Approved
**Scope:** pb-proxy model routing (dual-mode: aliases + passthrough)

---

## 1. Problem

The proxy currently requires every model to be listed in `litellm_config.yaml`.
This causes:

- **Fragile model IDs** — wrong IDs (e.g., `claude-3-5-haiku-20241022`) cause
  runtime `not_found_error` with no fallback
- **Manual maintenance** — every new model requires a config change + restart
- **No flexibility** — users can't try new models without proxy config changes

## 2. Solution: Dual-Mode Routing

Support both curated aliases from `litellm_config.yaml` AND passthrough routing
for any `provider/model` format string.

### Routing flow

```
Client sends model="X"
         │
         ▼
  ┌─ Is X in known aliases? ──┐
  │ YES (e.g. "claude-opus")  │ NO (e.g. "anthropic/claude-3-5-haiku-...")
  │                           │
  ▼                           ▼
Router.acompletion()    litellm.acompletion()
(litellm_config.yaml)         │
                              ├─ Extract provider prefix ("anthropic")
                              ├─ Resolve API key:
                              │   1. User Bearer token
                              │   2. Provider env var (ANTHROPIC_API_KEY)
                              │   3. → 401 error
                              └─ Direct LiteLLM call
```

### API key resolution (passthrough path)

1. **User-provided Bearer token** — `Authorization: Bearer sk-ant-...` header
2. **Provider env var** — convention-based lookup: provider `"anthropic"` →
   `ANTHROPIC_API_KEY`, `"openai"` → `OPENAI_API_KEY`, `"github"` → `GITHUB_PAT`
3. **Reject** — 401 with clear message: "No API key for provider 'X'"

The env vars are already read via `config._read_secret()` which supports Docker
Secrets with env var fallback.

### OPA policy

No changes needed. The existing `kb.proxy.provider_allowed` checks `agent_role`,
not specific model names. Passthrough is allowed for any role that can use the
proxy (analyst, developer, admin).

## 3. Approach

**Approach B: Pre-check Router model list.** Before calling the Router, check if
the model name exists in the known aliases set. If yes → Router. If no → extract
provider prefix and call `litellm.acompletion` directly.

### Alternatives considered

| Approach | Description | Trade-off |
|----------|-------------|-----------|
| A. Exception fallback | Try Router, catch model-not-found, retry direct | Ugly exception-based control flow |
| **B. Pre-check (chosen)** | Check alias set first, branch cleanly | Clean, explicit, minimal code |
| C. Router subclass | Subclass `litellm.Router` | Couples to LiteLLM internals |

## 4. Changes by file

### `pb-proxy/proxy.py`
- Store `router_acompletion` and `direct_acompletion` separately
- Build `known_aliases: set[str]` from `model_list` at startup
- New `_resolve_provider_key(provider, user_api_key)` helper
- `chat_completions`: branch on `model in known_aliases`
- `/v1/models`: unchanged (passthrough models are dynamic)

### `pb-proxy/config.py`
- New `PROVIDER_KEY_MAP: dict[str, str]` — auto-discovered mapping from provider
  prefix to env var name (e.g., `{"anthropic": "ANTHROPIC_API_KEY"}`)
- Only includes providers with a configured key

### `pb-proxy/litellm_config.yaml`
- Remove broken Haiku/Sonnet entries (they become passthrough)
- Keep curated aliases that add value (e.g., `"claude-opus"`)

### `opencode.jsonc`
- Add passthrough model entries (e.g., `"anthropic/claude-3-5-haiku-20241022"`)
- Keep short aliases (e.g., `"claude-opus"`)

### `pb-proxy/agent_loop.py`
- No changes (already receives a callable)

### OPA policies
- No changes

### Tests
- Router path (known alias resolves correctly)
- Passthrough path (provider/model format → direct call)
- Key resolution order (user key → env var → 401)
- Invalid model format (no prefix, not an alias → 400)

## 5. Error handling

| Case | HTTP | Message |
|------|------|---------|
| Known alias, works | 200 | Normal response |
| Known alias, LLM error | 502 | "LLM request failed" |
| `provider/model`, key found | 200 | Normal response |
| `provider/model`, no key | 401 | "No API key configured for provider 'X'" |
| No prefix + not an alias | 400 | "Unknown model 'X'. Use 'provider/model' format or a configured alias" |
| LiteLLM provider error | 502 | Provider error forwarded |

## 6. Non-goals

- Dynamic `/v1/models` listing for passthrough providers (would require provider
  API calls; clients already know their models)
- Provider-specific rate limiting (existing LiteLLM Router handles this for
  aliases; passthrough relies on provider-side limits)
- Multi-key rotation per provider (use LiteLLM Router aliases for that)
