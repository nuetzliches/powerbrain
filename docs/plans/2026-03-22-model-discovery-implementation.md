# Model Discovery / Wildcard Routing — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable the proxy to route any `provider/model` request directly via LiteLLM, while preserving curated aliases from `litellm_config.yaml`.

**Architecture:** Dual-mode routing — requests for known aliases go through `litellm.Router`, unknown models with a `provider/` prefix go through `litellm.acompletion` directly. API keys are resolved from the user's Bearer token first, then from provider-specific env vars.

**Tech Stack:** Python 3.12, FastAPI, LiteLLM, pytest

---

### Task 1: Add PROVIDER_KEY_MAP to config.py

**Files:**
- Modify: `pb-proxy/config.py:62-71`
- Test: `pb-proxy/tests/test_proxy.py` (tested indirectly in Task 4)

**Step 1: Add the provider key map**

Add a `PROVIDER_KEY_MAP` dict that auto-discovers which providers have API keys configured. Add it after the existing key exports:

```python
# ── Provider Key Map (for passthrough routing) ───────────────
# Maps LiteLLM provider prefix → env var value.
# Only providers with a configured key are included.
# Used by passthrough routing to resolve API keys for models
# not listed in litellm_config.yaml.
PROVIDER_KEY_MAP: dict[str, str] = {}

_PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "github": "GITHUB_PAT",
    "azure": "AZURE_API_KEY",
    "cohere": "COHERE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

for _provider, _env_var in _PROVIDER_ENV_VARS.items():
    _key = _read_secret(_env_var, "")
    if _key:
        PROVIDER_KEY_MAP[_provider] = _key
        # Also export to os.environ for LiteLLM (if not already set)
        if _env_var not in os.environ:
            os.environ[_env_var] = _key
```

This replaces the existing individual key exports (GITHUB_PAT, ANTHROPIC_API_KEY blocks).

**Step 2: Commit**

```bash
git add pb-proxy/config.py
git commit -m "feat(proxy): add PROVIDER_KEY_MAP for passthrough routing"
```

---

### Task 2: Refactor _load_llm_router to support dual-mode

**Files:**
- Modify: `pb-proxy/proxy.py:83-88,133-157,162-167`
- Test: `pb-proxy/tests/test_proxy.py`

**Step 1: Write the failing test**

Add to `test_proxy.py`:

```python
def test_passthrough_model_uses_direct_completion(mock_deps):
    """Models with provider/ prefix bypass Router and use litellm.acompletion."""
    import proxy
    original_aliases = proxy.known_aliases
    original_direct = proxy.direct_acompletion
    proxy.known_aliases = {"claude-opus", "gpt-4o"}
    proxy.direct_acompletion = mock_deps["acompletion"]

    try:
        from proxy import app
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "anthropic/claude-3-5-haiku-20241022",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200
        # Verify the model name was passed through to the loop
        run_call = mock_deps["loop"].run.call_args
        assert run_call.kwargs["model"] == "anthropic/claude-3-5-haiku-20241022"
    finally:
        proxy.known_aliases = original_aliases
        proxy.direct_acompletion = original_direct


def test_alias_model_uses_router(mock_deps):
    """Models matching a known alias use the Router."""
    import proxy
    original_aliases = proxy.known_aliases
    proxy.known_aliases = {"claude-opus", "gpt-4o"}

    try:
        from proxy import app
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-opus",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200
        run_call = mock_deps["loop"].run.call_args
        assert run_call.kwargs["model"] == "claude-opus"
    finally:
        proxy.known_aliases = original_aliases
```

**Step 2: Run tests to verify they fail**

```bash
cd pb-proxy && python3 -m pytest tests/test_proxy.py::test_passthrough_model_uses_direct_completion tests/test_proxy.py::test_alias_model_uses_router -v
```

Expected: FAIL — `proxy.known_aliases` does not exist.

**Step 3: Refactor proxy.py globals and _load_llm_router**

Update the globals section:

```python
# ── Globals ──────────────────────────────────────────────────

tool_injector = ToolInjector()
http_client: httpx.AsyncClient | None = None
pii_http_client: httpx.AsyncClient | None = None
router_acompletion: Any = None      # LiteLLM Router (for aliases)
direct_acompletion: Any = None      # LiteLLM direct (for passthrough)
model_list: list[dict[str, Any]] = []
known_aliases: set[str] = set()     # Model names configured in Router
```

Update `_load_llm_router`:

```python
def _load_llm_router() -> tuple[Any | None, Any, list[dict[str, Any]], set[str]]:
    """Load LiteLLM Router from YAML config + direct fallback.

    Returns (router_acompletion_or_None, direct_acompletion, model_list, known_aliases).
    """
    import litellm

    config_path = config.LITELLM_CONFIG
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("LiteLLM config not found at %s, using direct completion only", config_path)
        return None, litellm.acompletion, [], set()

    models = cfg.get("model_list", [])
    if not models:
        log.info("LiteLLM config has empty model_list, using direct completion only")
        return None, litellm.acompletion, [], set()

    router = litellm.Router(model_list=models)
    aliases = {m.get("model_name", "") for m in models}
    log.info("LiteLLM Router loaded with %d alias(es): %s", len(aliases), sorted(aliases))
    log.info("Passthrough routing enabled for providers: %s", sorted(config.PROVIDER_KEY_MAP.keys()))
    return router.acompletion, litellm.acompletion, models, aliases
```

Update `lifespan`:

```python
    global http_client, pii_http_client, router_acompletion, direct_acompletion, model_list, known_aliases
    http_client = httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    pii_http_client = httpx.AsyncClient(timeout=10)
    router_acompletion, direct_acompletion, model_list, known_aliases = _load_llm_router()
```

**Step 4: Update chat_completions to branch on alias vs passthrough**

Add a helper function before `chat_completions`:

```python
def _resolve_provider_key(model: str, user_api_key: str | None) -> tuple[Any, dict[str, Any]]:
    """Determine which acompletion callable + extra kwargs to use.

    For known aliases: use Router.
    For provider/model format: use direct litellm.acompletion with resolved key.
    Returns (acompletion_callable, extra_kwargs).
    Raises HTTPException if model can't be routed.
    """
    extra_kwargs: dict[str, Any] = {}

    if model in known_aliases:
        acompletion = router_acompletion or direct_acompletion
        if user_api_key:
            extra_kwargs["api_key"] = user_api_key
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

    # Resolve API key: user token → provider env var → reject
    if user_api_key:
        extra_kwargs["api_key"] = user_api_key
    elif provider in config.PROVIDER_KEY_MAP:
        extra_kwargs["api_key"] = config.PROVIDER_KEY_MAP[provider]
    else:
        raise HTTPException(
            status_code=401,
            detail=f"No API key configured for provider '{provider}'. "
                   f"Send your key via Authorization header or configure "
                   f"{provider.upper()}_API_KEY as env var / Docker Secret.",
        )

    return direct_acompletion, extra_kwargs
```

Then in `chat_completions`, replace the `user_api_key` handling and AgentLoop creation:

Replace:
```python
    # Pass user-provided API key to LiteLLM (overrides centrally configured key)
    if user_api_key:
        litellm_kwargs["api_key"] = user_api_key

    # Run agent loop
    loop = AgentLoop(
        tool_injector,
        acompletion=llm_acompletion,
        ...
    )
```

With:
```python
    # Resolve routing: alias → Router, provider/model → direct
    try:
        acompletion, routing_kwargs = _resolve_provider_key(model=request.model, user_api_key=user_api_key)
    except HTTPException:
        raise
    litellm_kwargs.update(routing_kwargs)

    # Run agent loop
    loop = AgentLoop(
        tool_injector,
        acompletion=acompletion,
        ...
    )
```

**Step 5: Run tests to verify they pass**

```bash
cd pb-proxy && python3 -m pytest tests/test_proxy.py -v
```

Expected: ALL PASS

**Step 6: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/tests/test_proxy.py
git commit -m "feat(proxy): dual-mode routing — aliases via Router, passthrough via direct"
```

---

### Task 3: Add passthrough-specific tests

**Files:**
- Modify: `pb-proxy/tests/test_proxy.py`

**Step 1: Write key resolution tests**

```python
def test_passthrough_no_key_returns_401(mock_deps):
    """Passthrough model with no API key returns 401."""
    import proxy
    original_aliases = proxy.known_aliases
    proxy.known_aliases = {"claude-opus"}
    try:
        from proxy import app
        client = TestClient(app)
        with patch.dict("config.PROVIDER_KEY_MAP", {}, clear=True):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "cohere/command-r-plus",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
            assert response.status_code == 401
            assert "cohere" in response.json()["detail"].lower()
    finally:
        proxy.known_aliases = original_aliases


def test_passthrough_user_key_overrides_env(mock_deps):
    """User Bearer token is used even when env var key exists."""
    import proxy
    original_aliases = proxy.known_aliases
    proxy.known_aliases = set()
    try:
        from proxy import app
        client = TestClient(app)
        with patch.dict("config.PROVIDER_KEY_MAP", {"anthropic": "central-key"}):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "anthropic/claude-opus-4-20250514",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                headers={"Authorization": "Bearer sk-ant-user-key-override"},
            )
            assert response.status_code == 200
            run_call = mock_deps["loop"].run.call_args
            assert run_call.kwargs.get("api_key") == "sk-ant-user-key-override"
    finally:
        proxy.known_aliases = original_aliases


def test_unknown_model_no_prefix_returns_400(mock_deps):
    """Model without provider prefix and not an alias returns 400."""
    import proxy
    original_aliases = proxy.known_aliases
    proxy.known_aliases = {"claude-opus"}
    try:
        from proxy import app
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "some-random-model",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 400
        assert "provider/model-name" in response.json()["detail"]
    finally:
        proxy.known_aliases = original_aliases
```

**Step 2: Run all tests**

```bash
cd pb-proxy && python3 -m pytest tests/ -v
```

Expected: ALL PASS

**Step 3: Commit**

```bash
git add pb-proxy/tests/test_proxy.py
git commit -m "test(proxy): add passthrough routing error case tests"
```

---

### Task 4: Clean up litellm_config.yaml

**Files:**
- Modify: `pb-proxy/litellm_config.yaml`

**Step 1: Remove broken entries, keep useful aliases**

Remove the Haiku and Sonnet entries (they're now reachable via passthrough).
Keep `claude-opus` and `gpt-4o` as convenient aliases:

```yaml
model_list:
  # ── GitHub Models (via PAT) ────────────────────────────────
  - model_name: "gpt-4o"
    litellm_params:
      model: "github/gpt-4o"
      api_key: "os.environ/GITHUB_PAT"

  - model_name: "gpt-4o-mini"
    litellm_params:
      model: "github/gpt-4o-mini"
      api_key: "os.environ/GITHUB_PAT"

  # ── Anthropic ──────────────────────────────────────────────
  - model_name: "claude-opus"
    litellm_params:
      model: "anthropic/claude-opus-4-20250514"
      api_key: "os.environ/ANTHROPIC_API_KEY"

  # Other Anthropic models (Sonnet, Haiku) are available via
  # passthrough routing: use "anthropic/<model-id>" directly.
  # No config entry needed — the proxy resolves the API key
  # from ANTHROPIC_API_KEY automatically.
```

**Step 2: Commit**

```bash
git add pb-proxy/litellm_config.yaml
git commit -m "chore(proxy): remove broken model entries, use passthrough routing"
```

---

### Task 5: Update opencode.jsonc

**Files:**
- Modify: `opencode.jsonc`

**Step 1: Add passthrough model examples**

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  // ── Powerbrain Proxy Provider ─────────────────────────────
  // Routes LLM requests through pb-proxy (PII pseudonymization +
  // Powerbrain tool injection).
  // Activate: docker compose --profile proxy up -d
  //
  // Models can use:
  //   - Short aliases from litellm_config.yaml (e.g. "claude-opus")
  //   - Full provider/model passthrough (e.g. "anthropic/claude-3-5-haiku-20241022")
  //
  // Auth: The proxy resolves API keys in order:
  //   1. Your Bearer token (set via /connect → Other → your API key)
  //   2. Central key from secrets/ (e.g. secrets/anthropic_api_key.txt)
  "provider": {
    "powerbrain-proxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Powerbrain Proxy",
      "options": {
        "baseURL": "http://localhost:8090/v1"
      },
      "models": {
        "claude-opus": {
          "name": "Claude Opus 4 (alias)",
          "limit": {
            "context": 200000,
            "output": 32000
          }
        },
        "anthropic/claude-sonnet-4-20250514": {
          "name": "Claude Sonnet 4 (passthrough)",
          "limit": {
            "context": 200000,
            "output": 16000
          }
        },
        "anthropic/claude-3-5-haiku-20241022": {
          "name": "Claude Haiku 3.5 (passthrough)",
          "limit": {
            "context": 200000,
            "output": 8192
          }
        },
        "gpt-4o": {
          "name": "GPT-4o (alias)",
          "limit": {
            "context": 128000,
            "output": 16384
          }
        }
      }
    }
  },
  "mcp": {
    "powerbrain": {
      "type": "remote",
      "url": "http://localhost:8080/mcp",
      "enabled": true,
      "oauth": false,
      "headers": {
        "Authorization": "Bearer kb_dev_localonly_do_not_use_in_production"
      }
    }
  }
}
```

**Step 2: Commit**

```bash
git add opencode.jsonc
git commit -m "chore: update opencode.jsonc with passthrough model examples"
```

---

### Task 6: Update documentation

**Files:**
- Modify: `CLAUDE.md` (update proxy section)
- Modify: `docs/plans/2026-03-21-ai-proxy-kb-api-design.md` (mark backlog item done)

**Step 1: Update CLAUDE.md**

In the MCP Tools section or proxy description, add a note about passthrough routing.

**Step 2: Mark backlog item as done**

In `docs/plans/2026-03-21-ai-proxy-kb-api-design.md`, change:
```
| Model discovery (wildcard/passthrough routing) | Medium | Proxy MVP |
```
to:
```
| ~~Model discovery (wildcard/passthrough routing)~~ | ~~Medium~~ | ✅ Implemented |
```

**Step 3: Commit**

```bash
git add CLAUDE.md docs/plans/2026-03-21-ai-proxy-kb-api-design.md
git commit -m "docs: update for passthrough routing"
```

---

### Task 7: Run full test suite and verify

**Step 1: Run all proxy tests**

```bash
cd pb-proxy && python3 -m pytest tests/ -v
```

Expected: ALL PASS (should be ~35+ tests)

**Step 2: Verify no regressions**

Check that existing alias routing, PII protection, streaming, and tool injection still work by reviewing test output.
