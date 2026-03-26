# Proxy Unified Auth & Provider Key Management Design

**Date:** 2026-03-25
**Status:** Approved
**Goal:** Secure all proxy endpoints behind global `pb_`-API-key authentication and enable flexible per-provider LLM API key management (central, user-provided, or hybrid) — Phase 1 uses headers only, Phase 2 (future) adds DB-stored keys.

## Context

The proxy currently has a **partial auth gap**: only `/v1/chat/completions` checks for a `pb_` API key (`proxy.py:401-416`). The `/v1/models`, `/metrics/json`, and `/health` endpoints are completely open. With the recent addition of `/metrics/json` (telemetry feature, commit `359c85e`), this gap is now a concrete security concern — operational metrics are exposed without authentication.

Additionally, all LLM provider API keys are centrally configured (env vars / Docker Secrets). There is no mechanism for users to supply their own provider keys, which blocks use cases like:
- Teams with separate provider billing
- Users with personal API keys for providers not centrally configured
- Hybrid setups where central keys serve as fallback

The MCP-Server already uses a global ASGI middleware stack (`server.py:1874-1900`) that cleanly separates auth from endpoint logic. The proxy should follow the same pattern for consistency and maintainability.

## Constraints

- **Proxy only** — no changes to Reranker, Ingestion, or MCP-Server authentication
- **Backward compatible** — `AUTH_REQUIRED=false` keeps everything open (dev setups); no `provider_keys` config = all providers default to `central` (current behavior)
- **No new user/person model** — existing `api_keys` table and `pb_` keys work as-is (keys remain flexible: personal or team-based)
- **Phase 1: headers only** — user-provided LLM keys via `X-Provider-Key` header, no DB storage
- **Phase 2: documented only** — per-user key storage in DB as a future extension
- `/health` remains unauthenticated (Docker health checks, load balancers)

## Design

### 1. Global Auth Middleware

Replace the manual auth check in `/v1/chat/completions` (`proxy.py:401-416`) with a global ASGI middleware applied to all routes, matching the MCP-Server pattern.

#### Middleware: `ProxyAuthMiddleware`

New file: `pb-proxy/middleware.py`

```python
class ProxyAuthMiddleware:
    """Global ASGI middleware for pb_ API key authentication.
    
    Pure ASGI middleware (not FastAPI-dependent). Applied to all routes
    except whitelisted paths. Uses raw ASGI send/receive — cannot use
    HTTPException (which is FastAPI-specific).
    
    On success: sets scope["state"]["agent_id"], scope["state"]["agent_role"],
                scope["state"]["bearer_token"].
    On failure: sends raw 401 JSON response with WWW-Authenticate header.
    """
    
    WHITELIST = {"/health"}  # No auth required
    
    def __init__(self, app, key_verifier, auth_required=True):
        self.app = app
        self.key_verifier = key_verifier
        self.auth_required = auth_required
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        
        path = scope.get("path", "")
        if path in self.WHITELIST or not self.auth_required:
            # Set defaults for unauthenticated / auth-disabled paths
            scope.setdefault("state", {})
            scope["state"]["agent_id"] = "anonymous"
            scope["state"]["agent_role"] = "developer"
            scope["state"]["bearer_token"] = None
            return await self.app(scope, receive, send)
        
        # Extract Bearer token from headers
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()
        bearer_token = None
        if auth_value.lower().startswith("bearer "):
            bearer_token = auth_value[7:].strip()
        
        if not bearer_token:
            return await self._send_401(send, "Authentication required")
        
        verified = await self.key_verifier.verify(bearer_token)
        if verified is None:
            return await self._send_401(send, "Invalid or expired API key")
        
        # Populate scope state for downstream handlers
        scope.setdefault("state", {})
        scope["state"]["agent_id"] = verified["agent_id"]
        scope["state"]["agent_role"] = verified["agent_role"]
        scope["state"]["bearer_token"] = bearer_token
        return await self.app(scope, receive, send)
    
    async def _send_401(self, send, detail: str):
        """Send a raw 401 JSON response (ASGI-level, no HTTPException)."""
        import json
        body = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"www-authenticate", b"Bearer"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body})
```

**Behavior:**
- Pure ASGI middleware — returns raw JSON responses on failure (consistent with MCP-Server's `RateLimitMiddleware` pattern in `server.py:240-287`)
- Extracts `Authorization: Bearer pb_...` from request headers
- Calls `ProxyKeyVerifier.verify()` (existing, `auth.py:51-97`)
- On success: populates `scope["state"]` dict — FastAPI's `Request.state` reads from this
- On failure: sends raw 401 JSON `{"detail": "..."}` with `WWW-Authenticate: Bearer` header
- Skips auth for whitelisted paths (`/health` only)
- When `AUTH_REQUIRED=false`: skips all checks, sets defaults (`agent_id="anonymous"`, `agent_role="developer"`)

**Integration in `proxy.py`:**
```python
from middleware import ProxyAuthMiddleware

# Register AFTER FastAPI app creation (before lifespan runs):
app = FastAPI(...)
app.add_middleware(ProxyAuthMiddleware,
                   key_verifier=key_verifier,
                   auth_required=config.AUTH_REQUIRED)
```

Note: `app.add_middleware()` is called at module level (app creation time), not inside the lifespan context manager. The `key_verifier.start()` call in the lifespan creates the DB pool — the middleware holds a reference to the verifier object, which is initialized before any requests arrive.

**Endpoint changes:**
- `/v1/chat/completions`: Remove inline auth block (lines 396-421). Read identity from `raw_request.state.agent_id` / `raw_request.state.agent_role` / `raw_request.state.bearer_token`.
- `/v1/models`: Now protected automatically (no code change needed — middleware handles it).
- `/metrics/json`: Now protected automatically.
- `/health`: Whitelisted, remains open.

### 2. Provider Key Handling

#### Key Source Configuration

Each provider can declare a `key_source` that controls how LLM API keys are resolved:

| `key_source` | Behavior | `X-Provider-Key` header |
|---|---|---|
| `central` (default) | Use centrally configured key (env var / Docker Secret) | Ignored |
| `user` | Require user-supplied key via header | Required — 422 if missing |
| `hybrid` | Use header key if present, fall back to central | Optional override |

Default when not configured: **`central`** — backward compatible, nothing changes.

#### `X-Provider-Key` Header

Single header for all providers. The proxy determines which provider from the `model` field in the request:
- Alias models: provider extracted from `litellm_config.yaml` model params (e.g., `"github/gpt-4o"` → `github`)
- Passthrough models: provider is the prefix (e.g., `"anthropic/claude-opus-4-20250514"` → `anthropic`)

#### Resolution Order

```
1. X-Provider-Key header present?
   ├─ key_source=user    → USE header key (required)
   ├─ key_source=hybrid  → USE header key (overrides central)
   └─ key_source=central → IGNORE header key, use central

2. No X-Provider-Key header:
   ├─ key_source=user    → 422 error
   ├─ key_source=hybrid  → FALL BACK to central key
   └─ key_source=central → USE central key (default, current behavior)
```

#### Provider Extraction

New helper function to extract the LiteLLM provider name from any model identifier:

```python
def _extract_provider(model: str) -> str:
    """Extract provider name from model identifier.
    
    - Alias models: look up litellm_params.model in config, extract prefix
      e.g. "gpt-4o" → config says "github/gpt-4o" → "github"
    - Passthrough models: split on "/" → first segment
      e.g. "anthropic/claude-opus-4-20250514" → "anthropic"
    - Unknown aliases without provider prefix: return model name as-is
    """
    if model in known_aliases:
        # Look up the underlying provider/model from litellm_config
        for entry in model_list:
            if entry.get("model_name") == model:
                underlying = entry.get("litellm_params", {}).get("model", "")
                if "/" in underlying:
                    return underlying.split("/")[0]
        return model  # Alias without resolvable provider
    
    if "/" in model:
        return model.split("/")[0]
    
    return model
```

#### Implementation

Modify `_resolve_provider_key()` (`proxy.py:349-386`) to accept an optional `user_provider_key: str | None` parameter and consult the `key_source` config:

```python
# Module-level global (populated by _load_llm_router):
provider_key_config: dict[str, dict[str, str]] = {}
# Example value: {"openai": {"key_source": "user"}, "github": {"key_source": "hybrid"}}

def _resolve_provider_key(
    model: str, 
    user_provider_key: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve LLM routing + API key based on model and key_source config."""
    ...
    provider = _extract_provider(model)
    key_source = provider_key_config.get(provider, {}).get("key_source", "central")
    
    if key_source == "user":
        if not user_provider_key:
            raise HTTPException(
                status_code=422,
                detail=f"Provider '{provider}' requires user-supplied key "
                       f"via X-Provider-Key header",
            )
        extra_kwargs["api_key"] = user_provider_key
    elif key_source == "hybrid":
        extra_kwargs["api_key"] = user_provider_key or config.PROVIDER_KEY_MAP.get(provider)
        if not extra_kwargs.get("api_key"):
            raise HTTPException(
                status_code=401,
                detail=f"No API key available for provider '{provider}'",
            )
    else:  # central (default)
        # Current behavior: use PROVIDER_KEY_MAP
        if provider in config.PROVIDER_KEY_MAP:
            extra_kwargs["api_key"] = config.PROVIDER_KEY_MAP[provider]
        else:
            raise HTTPException(
                status_code=401,
                detail=f"No API key configured for provider '{provider}'. "
                       f"Configure {provider.upper()}_API_KEY as env var / Docker Secret.",
            )
    ...
```

### 3. Configuration

#### `litellm_config.yaml` Extension

Add an optional `provider_keys` top-level section:

```yaml
model_list:
  - model_name: "gpt-4o"
    litellm_params:
      model: "github/gpt-4o"
      api_key: "os.environ/GITHUB_PAT"
  # ... existing entries ...

# Optional: per-provider key source configuration
# If omitted entirely, all providers default to "central" (current behavior).
provider_keys:
  # Central-only: proxy uses configured env var / Docker Secret (default)
  anthropic:
    key_source: central

  # User-required: user MUST supply key via X-Provider-Key header
  openai:
    key_source: user

  # Hybrid: user key takes precedence, falls back to central
  github:
    key_source: hybrid
```

#### Config Loading

Extend `_load_llm_router()` (`proxy.py:175-199`) to also parse the `provider_keys` section from the YAML config. Return type changes from 4-tuple to 5-tuple:

```python
def _load_llm_router() -> tuple[Any | None, Any, list[dict], set[str], dict[str, dict]]:
    """Load LiteLLM Router + provider key config from YAML.
    
    Returns (router_acompletion, direct_acompletion, model_list, 
             known_aliases, provider_key_config).
    """
    ...
    provider_keys = cfg.get("provider_keys", {})  # dict[str, dict[str, str]]
    return router.acompletion, litellm.acompletion, models, aliases, provider_keys
```

The module-level global `provider_key_config` is populated in the lifespan alongside `known_aliases`:
```python
global provider_key_config
router_acompletion, direct_acompletion, model_list, known_aliases, provider_key_config = _load_llm_router()
```

#### OPA Integration

**No new OPA policies needed.** The existing `pb.proxy.provider_allowed` policy already controls which roles can access which providers. Key-sourcing is a transport-layer concern below the policy layer.

The flow remains:
1. Auth middleware verifies `pb_` key → extracts `agent_role`
2. OPA checks `provider_allowed` for that role + provider
3. If allowed, `_resolve_provider_key()` handles key resolution based on `key_source`

### 4. Error Handling

Auth middleware errors use raw ASGI responses (middleware runs before FastAPI). All other errors use FastAPI `HTTPException` with `{"detail": "..."}` JSON body (existing pattern).

| Scenario | Layer | Status | Detail |
|---|---|---|---|
| No `Authorization` header | Middleware (ASGI) | **401** | `"Authentication required"` |
| Token doesn't start with `pb_` | Middleware (ASGI) | **401** | `"Invalid or expired API key"` |
| Token not found / expired / inactive | Middleware (ASGI) | **401** | `"Invalid or expired API key"` |
| `key_source=user`, no `X-Provider-Key` | Endpoint (HTTPException) | **422** | `"Provider '<name>' requires user-supplied key via X-Provider-Key header"` |
| `key_source=hybrid`, no header, no central key | Endpoint (HTTPException) | **401** | `"No API key available for provider '<name>'"` |
| LLM rejects provided key | Endpoint (HTTPException) | **502** | `"LLM request failed"` (existing) |
| Request to `/health` (whitelisted) | N/A | **200** | Normal response |
| `AUTH_REQUIRED=false` | N/A | N/A | All endpoints open |

The middleware returns `WWW-Authenticate: Bearer` header on 401 responses for HTTP spec compliance. Both layers produce identical `{"detail": "..."}` JSON format for client consistency.

### 5. Testing Strategy

#### Unit Tests (`pb-proxy/tests/`)

| Test | Validates |
|---|---|
| `test_middleware_rejects_no_token` | 401 on missing Authorization header |
| `test_middleware_rejects_bad_token` | 401 on invalid `pb_` key |
| `test_middleware_passes_valid_token` | Identity available on `request.state` |
| `test_middleware_skips_health` | `/health` accessible without auth |
| `test_middleware_protects_models` | `/v1/models` requires valid token |
| `test_middleware_protects_metrics` | `/metrics/json` requires valid token |
| `test_middleware_disabled` | `AUTH_REQUIRED=false` skips all checks |
| `test_provider_key_central` | Central key used, `X-Provider-Key` ignored |
| `test_provider_key_user_present` | User header key used for LLM call |
| `test_provider_key_user_missing` | 422 when `key_source=user` and no header |
| `test_provider_key_hybrid_override` | Header key overrides central when present |
| `test_provider_key_hybrid_fallback` | Central key used when no header in hybrid mode |
| `test_provider_key_default_central` | Unconfigured providers default to `central` |

#### Integration Tests (`tests/integration/e2e/`)

| Test | Validates |
|---|---|
| `test_proxy_auth_required` | Full stack: proxy rejects unauthenticated request |
| `test_proxy_auth_accepted` | Full stack: proxy accepts valid `pb_` key |
| `test_proxy_models_authenticated` | `/v1/models` returns model list with valid key |

#### OPA Tests

No new OPA policies — existing `pb.proxy` tests remain unchanged.

### 6. Rollout Plan

**Phase 1 (this implementation):**
1. Add `ProxyAuthMiddleware` as global ASGI middleware (whitelist: `/health` only)
2. Remove manual auth check from `/v1/chat/completions` endpoint
3. Add `provider_keys` config section to `litellm_config.yaml`
4. Implement `X-Provider-Key` header handling in `_resolve_provider_key()`
5. Unit tests + integration tests

**Phase 2 (future, documented only):**
- Per-user provider key storage in PostgreSQL (encrypted at rest)
- Key encryption with Fernet / AES-GCM
- Admin API for key CRUD operations
- Audit logging for provider key usage
- UI for key management

### 7. Files Changed

| File | Change |
|---|---|
| `pb-proxy/middleware.py` | **NEW** — `ProxyAuthMiddleware` class |
| `pb-proxy/proxy.py` | Remove inline auth from `/v1/chat/completions`; read identity from `request.state`; register middleware; extend `_resolve_provider_key()` with `key_source` + `X-Provider-Key` support; load `provider_keys` config |
| `pb-proxy/config.py` | Add `PROVIDER_KEY_CONFIG` dict loaded from `litellm_config.yaml` |
| `pb-proxy/litellm_config.yaml` | Add commented `provider_keys` section with examples |
| `pb-proxy/tests/test_middleware.py` | **NEW** — Unit tests for auth middleware |
| `pb-proxy/tests/test_provider_keys.py` | **NEW** — Unit tests for provider key resolution |
| `tests/integration/e2e/test_smoke.py` | Add proxy auth integration tests |
| `CLAUDE.md` | Update proxy auth documentation |

### 8. Backward Compatibility

| Scenario | Behavior |
|---|---|
| `AUTH_REQUIRED=false` (dev) | All endpoints open, no auth checks — identical to current |
| `AUTH_REQUIRED=true`, no `provider_keys` config | All providers use central keys — identical to current |
| Existing `pb_` keys | Work unchanged — same table, same verification |
| `/health` | Always unauthenticated — Docker health checks unaffected |
| Clients not sending `X-Provider-Key` | All providers defaulting to `central` work exactly as before |
