# Proxy Unified Auth & Provider Key Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Secure all proxy endpoints behind global `pb_`-API-key middleware and enable per-provider `key_source` (central/user/hybrid) with `X-Provider-Key` header support.

**Architecture:** Pure ASGI middleware (`ProxyAuthMiddleware`) intercepts all requests before FastAPI, validates `pb_` tokens via existing `ProxyKeyVerifier`, and populates `scope["state"]` for downstream handlers. Provider key resolution uses a `provider_keys` config section in `litellm_config.yaml` with three modes. The `/v1/chat/completions` endpoint loses its inline auth block and reads identity from `request.state`.

**Tech Stack:** Python 3.12, FastAPI, ASGI middleware, asyncpg (existing), pytest, unittest.mock

---

## File Inventory

### Create
- `pb-proxy/middleware.py` — `ProxyAuthMiddleware` (pure ASGI)
- `pb-proxy/tests/test_middleware.py` — Unit tests for auth middleware
- `pb-proxy/tests/test_provider_keys.py` — Unit tests for provider key resolution

### Modify
- `pb-proxy/proxy.py` — Register middleware, remove inline auth, extend `_resolve_provider_key()`, add `provider_key_config` global, add `_extract_provider()` helper
- `pb-proxy/litellm_config.yaml` — Add commented `provider_keys` section
- `pb-proxy/tests/test_proxy_auth.py` — Update existing tests to work with middleware-based auth
- `CLAUDE.md` — Update proxy auth documentation

---

### Task 1: ProxyAuthMiddleware — Tests and Implementation

**Files:**
- Create: `pb-proxy/middleware.py`
- Create: `pb-proxy/tests/test_middleware.py`

- [ ] **Step 1: Write failing tests for ProxyAuthMiddleware**

```python
# pb-proxy/tests/test_middleware.py
"""Tests for ProxyAuthMiddleware."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_scope(path="/v1/models", method="GET", headers=None):
    """Create a minimal ASGI HTTP scope."""
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append([k.lower().encode(), v.encode()])
    return {
        "type": "http",
        "path": path,
        "method": method,
        "headers": raw_headers,
        "state": {},
    }


async def _collect_response(middleware, scope):
    """Run middleware and collect response status + body."""
    response_started = {}
    response_body = b""

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        nonlocal response_body
        if message["type"] == "http.response.start":
            response_started["status"] = message["status"]
            response_started["headers"] = {
                k.decode(): v.decode()
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response_body = message.get("body", b"")

    await middleware(scope, receive, send)
    return response_started, response_body


@pytest.fixture
def mock_verifier():
    """Mock ProxyKeyVerifier that accepts pb_valid_key."""
    verifier = AsyncMock()

    async def _verify(token):
        if token == "pb_valid_key_123456789012345678901":
            return {"agent_id": "test-agent", "agent_role": "analyst"}
        return None

    verifier.verify = AsyncMock(side_effect=_verify)
    return verifier


@pytest.fixture
def mock_app():
    """Mock downstream ASGI app that records scope state."""
    calls = []

    async def app(scope, receive, send):
        calls.append(dict(scope.get("state", {})))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok": true}'})

    app.calls = calls
    return app


class TestMiddlewareAuthRequired:
    """Tests with auth_required=True."""

    def _make_middleware(self, app, verifier):
        from middleware import ProxyAuthMiddleware
        return ProxyAuthMiddleware(app, key_verifier=verifier, auth_required=True)

    @pytest.mark.asyncio
    async def test_rejects_no_auth_header(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/v1/models")
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401
        assert json.loads(body)["detail"] == "Authentication required"
        assert resp["headers"]["www-authenticate"] == "Bearer"
        assert len(mock_app.calls) == 0

    @pytest.mark.asyncio
    async def test_rejects_invalid_key(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(
            path="/v1/models",
            headers={"Authorization": "Bearer pb_bad_key_does_not_exist_1234567"},
        )
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401
        assert json.loads(body)["detail"] == "Invalid or expired API key"

    @pytest.mark.asyncio
    async def test_passes_valid_key(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(
            path="/v1/models",
            headers={"Authorization": "Bearer pb_valid_key_123456789012345678901"},
        )
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200
        assert len(mock_app.calls) == 1
        assert mock_app.calls[0]["agent_id"] == "test-agent"
        assert mock_app.calls[0]["agent_role"] == "analyst"
        assert mock_app.calls[0]["bearer_token"] == "pb_valid_key_123456789012345678901"

    @pytest.mark.asyncio
    async def test_skips_health_endpoint(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/health")
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200
        assert len(mock_app.calls) == 1
        # Health endpoint gets anonymous defaults
        assert mock_app.calls[0]["agent_id"] == "anonymous"
        mock_verifier.verify.assert_not_called()

    @pytest.mark.asyncio
    async def test_protects_metrics_json(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/metrics/json")
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401

    @pytest.mark.asyncio
    async def test_protects_v1_models(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/v1/models")
        resp, body = await _collect_response(mw, scope)
        assert resp["status"] == 401

    @pytest.mark.asyncio
    async def test_ignores_non_http_scope(self, mock_app, mock_verifier):
        """WebSocket and lifespan scopes pass through without auth."""
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = {"type": "lifespan"}
        # Should just call through to app
        calls = []
        async def passthrough_app(scope, receive, send):
            calls.append(True)
        mw_pass = self._make_middleware(passthrough_app, mock_verifier)
        await mw_pass(scope, None, None)
        assert len(calls) == 1


class TestMiddlewareAuthDisabled:
    """Tests with auth_required=False."""

    def _make_middleware(self, app, verifier):
        from middleware import ProxyAuthMiddleware
        return ProxyAuthMiddleware(app, key_verifier=verifier, auth_required=False)

    @pytest.mark.asyncio
    async def test_allows_anonymous(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/v1/models")
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200
        assert mock_app.calls[0]["agent_id"] == "anonymous"
        assert mock_app.calls[0]["agent_role"] == "developer"
        mock_verifier.verify.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_metrics_anonymous(self, mock_app, mock_verifier):
        mw = self._make_middleware(mock_app, mock_verifier)
        scope = _make_scope(path="/metrics/json")
        resp, _ = await _collect_response(mw, scope)
        assert resp["status"] == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest pb-proxy/tests/test_middleware.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'middleware'`

- [ ] **Step 3: Implement ProxyAuthMiddleware**

```python
# pb-proxy/middleware.py
"""
Proxy authentication middleware.
Pure ASGI middleware that validates pb_ API keys on all routes
except whitelisted paths (e.g. /health).
"""

import json
import logging

log = logging.getLogger("pb-proxy.middleware")


class ProxyAuthMiddleware:
    """Global ASGI middleware for pb_ API key authentication.

    Applied to all HTTP routes except whitelisted paths.
    On success: populates scope["state"] with agent_id, agent_role, bearer_token.
    On failure: sends 401 JSON response with WWW-Authenticate header.
    Non-HTTP scopes (lifespan, websocket) pass through unchanged.
    """

    WHITELIST = {"/health"}

    def __init__(self, app, key_verifier, auth_required: bool = True) -> None:
        self.app = app
        self.key_verifier = key_verifier
        self.auth_required = auth_required

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # Whitelisted paths and auth-disabled mode: set anonymous defaults
        if path in self.WHITELIST or not self.auth_required:
            scope.setdefault("state", {})
            scope["state"]["agent_id"] = "anonymous"
            scope["state"]["agent_role"] = "developer"
            scope["state"]["bearer_token"] = None
            return await self.app(scope, receive, send)

        # Extract Bearer token
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

        # Populate scope state for downstream FastAPI handlers
        scope.setdefault("state", {})
        scope["state"]["agent_id"] = verified["agent_id"]
        scope["state"]["agent_role"] = verified["agent_role"]
        scope["state"]["bearer_token"] = bearer_token

        log.info("Authenticated: agent_id=%s, agent_role=%s",
                 verified["agent_id"], verified["agent_role"])

        return await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send, detail: str) -> None:
        """Send a 401 JSON response at the ASGI level."""
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest pb-proxy/tests/test_middleware.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add pb-proxy/middleware.py pb-proxy/tests/test_middleware.py
git commit -m "feat(proxy): add ProxyAuthMiddleware with tests

Pure ASGI middleware that validates pb_ API keys on all routes
except /health. Populates scope state for downstream handlers."
```

---

### Task 2: Wire Middleware into Proxy — Replace Inline Auth

**Files:**
- Modify: `pb-proxy/proxy.py` (lines 76, 127, 238-243, 389-421)

- [ ] **Step 1: Write failing test for middleware integration**

The existing tests in `test_proxy_auth.py` verify auth behavior on `/v1/chat/completions`. After wiring the middleware, `/v1/models` and `/metrics/json` should also require auth. Add these tests to `test_middleware.py`:

```python
# Append to pb-proxy/tests/test_middleware.py

class TestMiddlewareWithFastAPI:
    """Integration tests: middleware + real FastAPI app."""

    @pytest.fixture
    def client(self, mock_verifier):
        """TestClient with middleware-protected proxy app."""
        from unittest.mock import patch, MagicMock
        import config as proxy_config

        with patch.object(proxy_config, "AUTH_REQUIRED", True), \
             patch("proxy.key_verifier", mock_verifier), \
             patch("proxy.tool_injector") as mock_inj:
            mock_inj.tool_names = set()
            mock_inj.server_names = []
            from proxy import app
            from fastapi.testclient import TestClient
            yield TestClient(app, raise_server_exceptions=False)

    def test_models_requires_auth(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 401

    def test_models_with_valid_key(self, client):
        resp = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer pb_valid_key_123456789012345678901"},
        )
        assert resp.status_code == 200

    def test_metrics_requires_auth(self, client):
        resp = client.get("/metrics/json")
        assert resp.status_code == 401

    def test_health_no_auth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run to verify tests fail**

Run: `python3 -m pytest pb-proxy/tests/test_middleware.py::TestMiddlewareWithFastAPI -v`
Expected: FAIL — `/v1/models` returns 200 (no middleware registered yet)

- [ ] **Step 3: Register middleware and remove inline auth**

In `pb-proxy/proxy.py`, make these changes:

**Add import** (after line 76 `from auth import ProxyKeyVerifier`):
```python
from middleware import ProxyAuthMiddleware
```

**Register middleware** (after line 243 `app = FastAPI(...)`):
```python
# Global auth middleware — protects all endpoints except /health
app.add_middleware(
    ProxyAuthMiddleware,
    key_verifier=key_verifier,
    auth_required=config.AUTH_REQUIRED,
)
```

**Remove inline auth block** in `chat_completions()` (lines 396-421). Replace with reading from `request.state`:
```python
        # ── Identity (set by ProxyAuthMiddleware) ─────────────────
        agent_id: str = raw_request.state.agent_id
        agent_role: str = raw_request.state.agent_role
        user_api_key: str | None = raw_request.state.bearer_token
```

**Remove conditional key_verifier start/stop from lifespan** — the middleware always holds the reference, so keep `key_verifier.start()` / `key_verifier.stop()` but remove the `if config.AUTH_REQUIRED:` guard (middleware handles the check):

Change lifespan (lines 213-217):
```python
    await key_verifier.start()
    if config.AUTH_REQUIRED:
        log.info("Proxy authentication enabled (AUTH_REQUIRED=true)")
    else:
        log.warning("Proxy authentication DISABLED (AUTH_REQUIRED=false)")
```

Change lifespan shutdown (line 231):
```python
    await key_verifier.stop()
```

- [ ] **Step 4: Run all proxy tests**

Run: `python3 -m pytest pb-proxy/tests/ -v`
Expected: All tests PASS (including updated `test_proxy_auth.py` and new middleware tests)

Note: The existing `test_proxy_auth.py` tests for `/v1/chat/completions` auth should still pass because the middleware now handles what the inline code used to do. If any tests fail due to the middleware intercepting before the mock patches take effect, update them to use the middleware-aware pattern (patching `proxy.key_verifier` which the middleware uses).

- [ ] **Step 5: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/tests/test_middleware.py
git commit -m "feat(proxy): wire ProxyAuthMiddleware, remove inline auth

All endpoints except /health now require pb_ API key when
AUTH_REQUIRED=true. Removes manual auth block from
/v1/chat/completions endpoint."
```

---

### Task 3: Provider Key Config — Loading and `_extract_provider()`

**Files:**
- Modify: `pb-proxy/proxy.py` (lines 175-199, add helper)
- Modify: `pb-proxy/litellm_config.yaml`
- Create: `pb-proxy/tests/test_provider_keys.py`

- [ ] **Step 1: Write failing tests for config loading and provider extraction**

```python
# pb-proxy/tests/test_provider_keys.py
"""Tests for provider key configuration and resolution."""

import pytest
from unittest.mock import patch, MagicMock


class TestExtractProvider:
    """Tests for _extract_provider() helper."""

    def test_passthrough_model(self):
        from proxy import _extract_provider
        assert _extract_provider("anthropic/claude-opus-4-20250514") == "anthropic"

    def test_passthrough_nested(self):
        from proxy import _extract_provider
        assert _extract_provider("openai/gpt-4o-2024-08-06") == "openai"

    def test_alias_resolved(self):
        """Alias models resolve provider from litellm config."""
        with patch("proxy.known_aliases", {"gpt-4o"}), \
             patch("proxy.model_list", [
                 {"model_name": "gpt-4o", "litellm_params": {"model": "github/gpt-4o"}},
             ]):
            from proxy import _extract_provider
            assert _extract_provider("gpt-4o") == "github"

    def test_alias_no_provider_prefix(self):
        """Alias without provider prefix in config returns model name."""
        with patch("proxy.known_aliases", {"local-llama"}), \
             patch("proxy.model_list", [
                 {"model_name": "local-llama", "litellm_params": {"model": "llama3.2"}},
             ]):
            from proxy import _extract_provider
            assert _extract_provider("local-llama") == "local-llama"

    def test_unknown_model_no_slash(self):
        """Unknown model without slash returns model name as-is."""
        with patch("proxy.known_aliases", set()), \
             patch("proxy.model_list", []):
            from proxy import _extract_provider
            assert _extract_provider("some-model") == "some-model"


class TestLoadProviderKeyConfig:
    """Tests for provider_keys loading from litellm_config.yaml."""

    def test_loads_provider_keys(self, tmp_path):
        import yaml
        config_file = tmp_path / "litellm_config.yaml"
        config_file.write_text(yaml.dump({
            "model_list": [
                {"model_name": "gpt-4o", "litellm_params": {"model": "github/gpt-4o"}},
            ],
            "provider_keys": {
                "anthropic": {"key_source": "central"},
                "openai": {"key_source": "user"},
                "github": {"key_source": "hybrid"},
            },
        }))
        with patch("config.LITELLM_CONFIG", str(config_file)):
            from proxy import _load_llm_router
            _, _, _, _, pkey_config = _load_llm_router()
        assert pkey_config["anthropic"]["key_source"] == "central"
        assert pkey_config["openai"]["key_source"] == "user"
        assert pkey_config["github"]["key_source"] == "hybrid"

    def test_missing_provider_keys_returns_empty(self, tmp_path):
        import yaml
        config_file = tmp_path / "litellm_config.yaml"
        config_file.write_text(yaml.dump({
            "model_list": [
                {"model_name": "gpt-4o", "litellm_params": {"model": "github/gpt-4o"}},
            ],
        }))
        with patch("config.LITELLM_CONFIG", str(config_file)):
            from proxy import _load_llm_router
            _, _, _, _, pkey_config = _load_llm_router()
        assert pkey_config == {}

    def test_missing_config_file_returns_empty(self):
        with patch("config.LITELLM_CONFIG", "/nonexistent/path.yaml"):
            from proxy import _load_llm_router
            _, _, _, _, pkey_config = _load_llm_router()
        assert pkey_config == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest pb-proxy/tests/test_provider_keys.py -v`
Expected: FAIL — `_extract_provider` not found, `_load_llm_router` returns 4-tuple

- [ ] **Step 3: Implement _extract_provider() and extend _load_llm_router()**

In `pb-proxy/proxy.py`:

**Add module-level global** (after line 126 `known_aliases: set[str] = set()`):
```python
provider_key_config: dict[str, dict[str, str]] = {}
```

**Add `_extract_provider()` helper** (before `_resolve_provider_key()`):
```python
def _extract_provider(model: str) -> str:
    """Extract LiteLLM provider name from model identifier.

    - Alias models: look up litellm_params.model in config, extract prefix
    - Passthrough models: split on "/" → first segment
    - Unknown aliases without provider prefix: return model name as-is
    """
    if model in known_aliases:
        for entry in model_list:
            if entry.get("model_name") == model:
                underlying = entry.get("litellm_params", {}).get("model", "")
                if "/" in underlying:
                    return underlying.split("/")[0]
        return model

    if "/" in model:
        return model.split("/")[0]

    return model
```

**Extend `_load_llm_router()` return type** — add `provider_keys` parsing:

Change the function signature and return:
```python
def _load_llm_router() -> tuple[Any | None, Any, list[dict[str, Any]], set[str], dict[str, dict[str, str]]]:
    """Load LiteLLM Router from YAML config + direct fallback.

    Returns (router_acompletion_or_None, direct_acompletion, model_list,
             known_aliases, provider_key_config).
    """
```

Add before the final return at the end of the function:
```python
    provider_keys = cfg.get("provider_keys", {})
```

Update all return statements:
- No models: `return None, litellm.acompletion, [], set(), {}`
- Empty models: `return None, litellm.acompletion, [], set(), provider_keys`
- Normal: `return router.acompletion, litellm.acompletion, models, aliases, provider_keys`

**Update lifespan** to unpack the 5th value (line 209):
```python
    router_acompletion, direct_acompletion, model_list, known_aliases, provider_key_config = _load_llm_router()
```

Also add `provider_key_config` to the `global` statement (line 206):
```python
    global http_client, pii_http_client, router_acompletion, direct_acompletion, model_list, known_aliases, provider_key_config
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest pb-proxy/tests/test_provider_keys.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Update litellm_config.yaml with commented example**

Append to `pb-proxy/litellm_config.yaml`:
```yaml

# ── Provider Key Source Configuration ────────────────────────
# Controls how LLM API keys are resolved per provider.
# Omit entirely to default all providers to "central" (current behavior).
#
# key_source options:
#   central  — Use env var / Docker Secret (default, current behavior)
#   user     — Require key via X-Provider-Key header (422 if missing)
#   hybrid   — Use X-Provider-Key if present, fall back to central
#
# provider_keys:
#   anthropic:
#     key_source: central
#   openai:
#     key_source: user
#   github:
#     key_source: hybrid
```

- [ ] **Step 6: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/litellm_config.yaml pb-proxy/tests/test_provider_keys.py
git commit -m "feat(proxy): add provider key config loading and _extract_provider

Extends _load_llm_router() to parse provider_keys section from
litellm_config.yaml. Adds _extract_provider() helper for resolving
provider name from both alias and passthrough model identifiers."
```

---

### Task 4: Provider Key Resolution — `X-Provider-Key` Header Support

**Files:**
- Modify: `pb-proxy/proxy.py` (`_resolve_provider_key()`, `chat_completions()`)
- Modify: `pb-proxy/tests/test_provider_keys.py`

- [ ] **Step 1: Write failing tests for key resolution with key_source modes**

```python
# Append to pb-proxy/tests/test_provider_keys.py

class TestResolveProviderKeyWithKeySource:
    """Tests for _resolve_provider_key() with key_source config."""

    @pytest.fixture(autouse=True)
    def setup_globals(self):
        """Set up module globals for each test."""
        with patch("proxy.known_aliases", {"gpt-4o", "claude-opus"}), \
             patch("proxy.model_list", [
                 {"model_name": "gpt-4o", "litellm_params": {"model": "github/gpt-4o"}},
                 {"model_name": "claude-opus", "litellm_params": {"model": "anthropic/claude-opus-4-20250514"}},
             ]), \
             patch("proxy.router_acompletion", MagicMock()), \
             patch("proxy.direct_acompletion", MagicMock()):
            yield

    def test_central_uses_central_key(self):
        """key_source=central: use PROVIDER_KEY_MAP, ignore header."""
        with patch("proxy.provider_key_config", {"anthropic": {"key_source": "central"}}), \
             patch("config.PROVIDER_KEY_MAP", {"anthropic": "sk-central-key"}):
            from proxy import _resolve_provider_key
            _, kwargs = _resolve_provider_key(
                "anthropic/claude-3-haiku",
                user_provider_key="sk-user-key",
            )
            assert kwargs["api_key"] == "sk-central-key"

    def test_central_ignores_user_key(self):
        """key_source=central: X-Provider-Key is ignored."""
        with patch("proxy.provider_key_config", {"github": {"key_source": "central"}}), \
             patch("config.PROVIDER_KEY_MAP", {"github": "ghp-central"}):
            from proxy import _resolve_provider_key
            _, kwargs = _resolve_provider_key(
                "gpt-4o",
                user_provider_key="ghp-user-override",
            )
            assert kwargs.get("api_key") is None  # Alias uses Router, no explicit key

    def test_user_requires_header(self):
        """key_source=user: 422 if no X-Provider-Key."""
        from fastapi import HTTPException
        with patch("proxy.provider_key_config", {"openai": {"key_source": "user"}}):
            from proxy import _resolve_provider_key
            with pytest.raises(HTTPException) as exc_info:
                _resolve_provider_key("openai/gpt-4o", user_provider_key=None)
            assert exc_info.value.status_code == 422
            assert "X-Provider-Key" in exc_info.value.detail

    def test_user_uses_header_key(self):
        """key_source=user: use provided header key."""
        with patch("proxy.provider_key_config", {"openai": {"key_source": "user"}}):
            from proxy import _resolve_provider_key
            _, kwargs = _resolve_provider_key(
                "openai/gpt-4o",
                user_provider_key="sk-user-provided",
            )
            assert kwargs["api_key"] == "sk-user-provided"

    def test_hybrid_prefers_header(self):
        """key_source=hybrid: header key overrides central."""
        with patch("proxy.provider_key_config", {"github": {"key_source": "hybrid"}}), \
             patch("config.PROVIDER_KEY_MAP", {"github": "ghp-central"}):
            from proxy import _resolve_provider_key
            _, kwargs = _resolve_provider_key(
                "github/gpt-4o-mini",
                user_provider_key="ghp-user-override",
            )
            assert kwargs["api_key"] == "ghp-user-override"

    def test_hybrid_falls_back_to_central(self):
        """key_source=hybrid: falls back to central when no header."""
        with patch("proxy.provider_key_config", {"github": {"key_source": "hybrid"}}), \
             patch("config.PROVIDER_KEY_MAP", {"github": "ghp-central"}):
            from proxy import _resolve_provider_key
            _, kwargs = _resolve_provider_key(
                "github/gpt-4o-mini",
                user_provider_key=None,
            )
            assert kwargs["api_key"] == "ghp-central"

    def test_hybrid_no_key_anywhere(self):
        """key_source=hybrid: 401 if no header AND no central key."""
        from fastapi import HTTPException
        with patch("proxy.provider_key_config", {"newprovider": {"key_source": "hybrid"}}), \
             patch("config.PROVIDER_KEY_MAP", {}):
            from proxy import _resolve_provider_key
            with pytest.raises(HTTPException) as exc_info:
                _resolve_provider_key("newprovider/some-model", user_provider_key=None)
            assert exc_info.value.status_code == 401

    def test_default_is_central(self):
        """Unconfigured provider defaults to central."""
        with patch("proxy.provider_key_config", {}), \
             patch("config.PROVIDER_KEY_MAP", {"anthropic": "sk-central"}):
            from proxy import _resolve_provider_key
            _, kwargs = _resolve_provider_key(
                "anthropic/claude-3-haiku",
                user_provider_key="sk-should-be-ignored",
            )
            assert kwargs["api_key"] == "sk-central"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest pb-proxy/tests/test_provider_keys.py::TestResolveProviderKeyWithKeySource -v`
Expected: FAIL — `_resolve_provider_key()` doesn't accept `user_provider_key` parameter

- [ ] **Step 3: Implement provider key resolution with key_source**

In `pb-proxy/proxy.py`, modify `_resolve_provider_key()`:

```python
def _resolve_provider_key(
    model: str,
    user_provider_key: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Determine which acompletion callable + extra kwargs to use.

    For known aliases: use Router (key embedded in Router config).
    For provider/model format: use direct litellm.acompletion with resolved key.
    Key resolution respects provider_key_config key_source setting.
    Returns (acompletion_callable, extra_kwargs).
    Raises HTTPException if model can't be routed or key is missing.
    """
    extra_kwargs: dict[str, Any] = {}

    if model in known_aliases:
        acompletion = router_acompletion or direct_acompletion
        # Alias models have keys embedded in Router config.
        # For key_source=user on alias providers, override via extra_kwargs:
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
        elif key_source == "hybrid" and user_provider_key:
            extra_kwargs["api_key"] = user_provider_key
        # central: no override, Router uses its embedded key
        return acompletion, extra_kwargs

    # Passthrough: model must be "provider/model-name" format
    if "/" not in model:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown model '{model}'. "
                f"Use 'provider/model-name' format (e.g. 'anthropic/claude-opus-4-20250514') "
                f"or one of the configured aliases: {sorted(known_aliases)}"
            ),
        )

    provider = model.split("/")[0]
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
        resolved_key = user_provider_key or config.PROVIDER_KEY_MAP.get(provider)
        if not resolved_key:
            raise HTTPException(
                status_code=401,
                detail=f"No API key available for provider '{provider}'",
            )
        extra_kwargs["api_key"] = resolved_key
    else:  # central (default)
        if provider in config.PROVIDER_KEY_MAP:
            extra_kwargs["api_key"] = config.PROVIDER_KEY_MAP[provider]
        else:
            raise HTTPException(
                status_code=401,
                detail=f"No API key configured for provider '{provider}'. "
                       f"Configure {provider.upper()}_API_KEY as env var / Docker Secret.",
            )

    return direct_acompletion, extra_kwargs
```

**Update `chat_completions()` to pass `X-Provider-Key` header** — after the identity reading section:
```python
        # Read X-Provider-Key header for user-supplied provider keys
        user_provider_key = raw_request.headers.get("x-provider-key")
```

And pass it to `_resolve_provider_key()`:
```python
        acompletion, routing_kwargs = _resolve_provider_key(
            model=request.model,
            user_provider_key=user_provider_key,
        )
```

- [ ] **Step 4: Run all provider key tests**

Run: `python3 -m pytest pb-proxy/tests/test_provider_keys.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run full proxy test suite**

Run: `python3 -m pytest pb-proxy/tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/tests/test_provider_keys.py
git commit -m "feat(proxy): implement X-Provider-Key header with key_source modes

Supports central (default), user, and hybrid key_source per provider.
Users can supply LLM provider keys via X-Provider-Key header when
configured. Backward compatible: unconfigured providers default to
central key resolution."
```

---

### Task 5: Update Existing Tests and Fix Regressions

**Files:**
- Modify: `pb-proxy/tests/test_proxy_auth.py`
- Modify: `pb-proxy/tests/test_proxy.py` (if affected)

- [ ] **Step 1: Run full proxy test suite to identify regressions**

Run: `python3 -m pytest pb-proxy/tests/ -v 2>&1`
Expected: Some tests in `test_proxy_auth.py` may fail because the middleware now intercepts auth before the endpoint-level mock patches take effect.

- [ ] **Step 2: Update test_proxy_auth.py for middleware-based auth**

The existing tests in `test_proxy_auth.py` use `patch("proxy.key_verifier", mock_verifier)`. Since the middleware is registered at module import time with the module-level `key_verifier` object, patching `proxy.key_verifier` should still work because the middleware holds a reference to the same object.

If tests fail, the fix is to ensure the middleware's `key_verifier` attribute points to the mock:

```python
# In test fixtures that need auth, patch both the module reference
# and the middleware's attribute:
with patch("proxy.key_verifier", mock_verifier):
    from proxy import app
    # The middleware was created with the real key_verifier object.
    # We need to update the middleware's reference too:
    for mw in app.user_middleware:
        if hasattr(mw, 'kwargs') and 'key_verifier' in mw.kwargs:
            mw.kwargs['key_verifier'] = mock_verifier
```

Alternatively, if the middleware stores it via `app.add_middleware(cls, key_verifier=verifier)`, FastAPI wraps it — in that case patching the module-level `key_verifier` works because `add_middleware` was called with the same object reference.

Investigate the actual failure and apply the minimal fix.

- [ ] **Step 3: Run full test suite again**

Run: `python3 -m pytest pb-proxy/tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit fixes**

```bash
git add pb-proxy/tests/
git commit -m "fix(proxy): update existing tests for middleware-based auth"
```

---

### Task 6: Update Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md proxy auth section**

In `CLAUDE.md`, update the AI Provider Proxy section to document:
- Global auth middleware (all endpoints protected except `/health`)
- `X-Provider-Key` header support
- `provider_keys` config section in `litellm_config.yaml`
- Key source modes: central, user, hybrid

Add to the **Authentication** subsection:
```markdown
**Global Auth Middleware:**
- All endpoints require `pb_` API key via `Authorization: Bearer pb_<key>` header
- Exception: `/health` (whitelisted for Docker health checks / load balancers)
- `AUTH_REQUIRED=false` disables all auth checks (backward compatible)

**Provider Key Sources:**
- Configurable per-provider via `provider_keys` section in `litellm_config.yaml`
- `key_source: central` (default) — uses env var / Docker Secret
- `key_source: user` — requires `X-Provider-Key` header (422 if missing)
- `key_source: hybrid` — uses `X-Provider-Key` if present, falls back to central
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update proxy auth and provider key documentation"
```

---

### Task 7: Final Verification

- [ ] **Step 1: Run full proxy test suite**

Run: `python3 -m pytest pb-proxy/tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run global test suite (non-integration)**

Run: `python3 -m pytest -v`
Expected: All tests PASS (no regressions in mcp-server, shared, etc.)

- [ ] **Step 3: Verify no import errors**

Run: `python3 -c "import sys; sys.path.insert(0, 'pb-proxy'); import middleware; import proxy; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Review diff**

Run: `git diff --stat HEAD~6` (or however many commits were made)
Review that only expected files were changed.
