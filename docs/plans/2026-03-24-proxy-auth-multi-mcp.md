# Proxy Auth + Multi-MCP-Server Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add API-key authentication to the proxy and enable multi-MCP-server tool aggregation with per-server auth, prefix-based namespacing, and OPA-controlled server access.

**Architecture:** The proxy reads the existing `api_keys` PostgreSQL table to verify `kb_` Bearer tokens. The user's key is forwarded to MCP servers with `auth: bearer` mode (defense-in-depth). Multiple MCP servers are configured via `mcp_servers.yaml` with per-server auth modes, tool prefixes, and availability requirements. OPA controls which servers each role may access.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, MCP SDK (Streamable HTTP), OPA/Rego, Docker Compose, pytest

**Design doc:** `docs/plans/2026-03-24-proxy-auth-multi-mcp-design.md`

---

### Task 1: Add asyncpg dependency + DB config

**Files:**
- Modify: `pb-proxy/requirements.txt`
- Modify: `pb-proxy/config.py:1-84`
- Modify: `docker-compose.yml:337-384`

**Step 1: Add asyncpg to requirements**

In `pb-proxy/requirements.txt`, add:
```
asyncpg>=0.30
```

**Step 2: Add DB + auth config to `config.py`**

Add after line 60 (PII section), before Provider Key Map:

```python
# ── Authentication ───────────────────────────────────────────
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").lower() == "true"
PG_HOST = os.getenv("PG_HOST", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DATABASE = os.getenv("PG_DATABASE", "powerbrain")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = _read_secret("PG_PASSWORD", "changeme")

# ── MCP Servers ──────────────────────────────────────────────
MCP_SERVERS_CONFIG = os.getenv("MCP_SERVERS_CONFIG", "/app/mcp_servers.yaml")
```

**Step 3: Add DB env vars + config volume to docker-compose**

In `docker-compose.yml`, pb-proxy service section, add environment variables:
```yaml
      AUTH_REQUIRED:           ${AUTH_REQUIRED:-true}
      PG_HOST:                 postgres
      PG_PORT:                 "5432"
      PG_DATABASE:             powerbrain
      PG_USER:                 postgres
      PG_PASSWORD:             ${PG_PASSWORD:-changeme}
      MCP_SERVERS_CONFIG:      /app/mcp_servers.yaml
```

Add to volumes section:
```yaml
      - ./pb-proxy/mcp_servers.yaml:/app/mcp_servers.yaml:ro
```

Add postgres to depends_on:
```yaml
      postgres:
        condition: service_healthy
```

Add secret:
```yaml
      - pg_password
```

**Step 4: Commit**

```bash
git add pb-proxy/requirements.txt pb-proxy/config.py docker-compose.yml
git commit -m "chore: add asyncpg dependency, DB config, and auth settings for proxy"
```

---

### Task 2: Implement ProxyKeyVerifier

**Files:**
- Create: `pb-proxy/auth.py`
- Create: `pb-proxy/tests/test_auth.py`

**Step 1: Write the failing test**

Create `pb-proxy/tests/__init__.py` (empty) and `pb-proxy/tests/test_auth.py`:

```python
"""Tests for ProxyKeyVerifier."""

import hashlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_pool():
    """Create a mock asyncpg connection pool."""
    pool = AsyncMock()
    return pool


@pytest.fixture
def verifier(mock_pool):
    """Create a ProxyKeyVerifier with mocked pool."""
    from auth import ProxyKeyVerifier
    v = ProxyKeyVerifier.__new__(ProxyKeyVerifier)
    v._pool = mock_pool
    v._cache = {}
    v._cache_ttl = 60
    return v


@pytest.mark.asyncio
async def test_verify_valid_key(verifier, mock_pool):
    """Valid key returns agent_id and agent_role."""
    key = "kb_test_valid_key_12345678901234567890"
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    mock_pool.fetchrow.return_value = {
        "agent_id": "test-agent",
        "agent_role": "developer",
    }

    result = await verifier.verify(key)

    assert result is not None
    assert result["agent_id"] == "test-agent"
    assert result["agent_role"] == "developer"
    mock_pool.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_verify_invalid_key(verifier, mock_pool):
    """Invalid key returns None."""
    mock_pool.fetchrow.return_value = None

    result = await verifier.verify("kb_invalid_key_does_not_exist")

    assert result is None


@pytest.mark.asyncio
async def test_verify_empty_key(verifier):
    """Empty key returns None without DB call."""
    result = await verifier.verify("")
    assert result is None


@pytest.mark.asyncio
async def test_verify_cached(verifier, mock_pool):
    """Second call uses cache, not DB."""
    key = "kb_cached_key_123456789012345678901234"
    mock_pool.fetchrow.return_value = {
        "agent_id": "cached-agent",
        "agent_role": "admin",
    }

    result1 = await verifier.verify(key)
    result2 = await verifier.verify(key)

    assert result1 == result2
    assert mock_pool.fetchrow.call_count == 1  # Only one DB call


@pytest.mark.asyncio
async def test_verify_non_kb_prefix(verifier):
    """Non-kb_ prefixed tokens are rejected immediately."""
    result = await verifier.verify("sk-ant-some-anthropic-key")
    assert result is None
```

**Step 2: Run test to verify it fails**

```bash
cd pb-proxy && python -m pytest tests/test_auth.py -v
```
Expected: FAIL (ImportError — `auth` module doesn't exist yet)

**Step 3: Implement ProxyKeyVerifier**

Create `pb-proxy/auth.py`:

```python
"""
Proxy API-key authentication.
Verifies kb_ API keys against the shared api_keys PostgreSQL table.
"""

import hashlib
import logging
import time
from typing import Any

import asyncpg

import config

log = logging.getLogger("pb-proxy.auth")

# Result type for verified keys
VerifiedKey = dict[str, str]  # {"agent_id": ..., "agent_role": ...}


class ProxyKeyVerifier:
    """Verifies API keys against PostgreSQL with in-memory caching."""

    def __init__(self, cache_ttl: int = 60) -> None:
        self._pool: asyncpg.Pool | None = None
        self._cache: dict[str, tuple[VerifiedKey | None, float]] = {}
        self._cache_ttl = cache_ttl

    async def start(self) -> None:
        """Create the connection pool."""
        self._pool = await asyncpg.create_pool(
            host=config.PG_HOST,
            port=config.PG_PORT,
            database=config.PG_DATABASE,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            min_size=1,
            max_size=5,
        )
        log.info("ProxyKeyVerifier connected to PostgreSQL")

    async def stop(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            log.info("ProxyKeyVerifier disconnected from PostgreSQL")

    async def verify(self, token: str) -> VerifiedKey | None:
        """Verify an API key. Returns {"agent_id": ..., "agent_role": ...} or None.

        - Rejects empty tokens and non-kb_ prefixed tokens immediately
        - Uses in-memory cache with TTL
        - Updates last_used_at (throttled, fire-and-forget)
        """
        if not token or not token.startswith("kb_"):
            return None

        # Check cache
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(key_hash)
        if cached is not None:
            result, timestamp = cached
            if time.monotonic() - timestamp < self._cache_ttl:
                return result

        # DB lookup
        if self._pool is None:
            log.error("ProxyKeyVerifier not started (no pool)")
            return None

        row = await self._pool.fetchrow(
            "SELECT agent_id, agent_role FROM api_keys "
            "WHERE key_hash = $1 AND active = true "
            "AND (expires_at IS NULL OR expires_at > now())",
            key_hash,
        )

        if row is None:
            self._cache[key_hash] = (None, time.monotonic())
            return None

        result: VerifiedKey = {
            "agent_id": row["agent_id"],
            "agent_role": row["agent_role"],
        }
        self._cache[key_hash] = (result, time.monotonic())

        # Update last_used_at (fire-and-forget, throttled)
        try:
            await self._pool.execute(
                "UPDATE api_keys SET last_used_at = now() "
                "WHERE key_hash = $1 AND (last_used_at IS NULL "
                "OR last_used_at < now() - interval '5 minutes')",
                key_hash,
            )
        except Exception:
            pass  # Non-critical

        return result

    def invalidate_cache(self) -> None:
        """Clear the entire cache (e.g., after downstream 401)."""
        self._cache.clear()
```

**Step 4: Run tests to verify they pass**

```bash
cd pb-proxy && python -m pytest tests/test_auth.py -v
```
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add pb-proxy/auth.py pb-proxy/tests/
git commit -m "feat(proxy): add ProxyKeyVerifier for API-key authentication"
```

---

### Task 3: Integrate auth into proxy.py

**Files:**
- Modify: `pb-proxy/proxy.py:12-41` (imports)
- Modify: `pb-proxy/proxy.py:82-91` (globals)
- Modify: `pb-proxy/proxy.py:164-187` (lifespan)
- Modify: `pb-proxy/proxy.py:277-306` (chat endpoint auth section)

**Step 1: Write the failing test**

Create `pb-proxy/tests/test_proxy_auth.py`:

```python
"""Tests for proxy auth integration."""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_verifier():
    """Mock ProxyKeyVerifier."""
    verifier = AsyncMock()
    verifier.verify = AsyncMock()
    return verifier


def _create_app(mock_verifier, auth_required=True):
    """Create a test app with mocked dependencies."""
    with patch("config.AUTH_REQUIRED", auth_required), \
         patch("proxy.key_verifier", mock_verifier), \
         patch("proxy.tool_injector") as mock_ti, \
         patch("proxy.router_acompletion", None), \
         patch("proxy.direct_acompletion", AsyncMock()), \
         patch("proxy.known_aliases", set()), \
         patch("proxy.http_client", AsyncMock()):
        mock_ti.tool_names = set()
        mock_ti.merge_tools.return_value = []
        from proxy import app
        return app


def test_auth_required_no_header():
    """Request without auth header returns 401 when AUTH_REQUIRED=true."""
    verifier = AsyncMock()
    app = _create_app(verifier, auth_required=True)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 401


def test_auth_required_invalid_key():
    """Request with invalid key returns 401."""
    verifier = AsyncMock()
    verifier.verify.return_value = None
    app = _create_app(verifier, auth_required=True)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer kb_invalid_key_12345678901234567890"},
    )
    assert response.status_code == 401
```

**Step 2: Run test to verify it fails**

```bash
cd pb-proxy && python -m pytest tests/test_proxy_auth.py -v
```
Expected: FAIL (key_verifier not defined in proxy module)

**Step 3: Implement auth integration in proxy.py**

Add import at line 34 (after pii_middleware imports):
```python
from auth import ProxyKeyVerifier
```

Add global after line 90:
```python
key_verifier = ProxyKeyVerifier()
```

In `lifespan()`, add after `pii_http_client` initialization (after line 168):
```python
    if config.AUTH_REQUIRED:
        await key_verifier.start()
        log.info("Proxy authentication enabled (AUTH_REQUIRED=true)")
    else:
        log.warning("Proxy authentication DISABLED (AUTH_REQUIRED=false)")
```

In `lifespan()` shutdown, add before `http_client.aclose()`:
```python
    if config.AUTH_REQUIRED:
        await key_verifier.stop()
```

Replace the auth section in `chat_completions()` (lines 282-296) with:

```python
    # ── Authentication ────────────────────────────────────────
    agent_id: str = "anonymous"
    agent_role: str = "developer"
    user_api_key: str | None = None  # For MCP server identity propagation

    auth_header = raw_request.headers.get("authorization", "")
    bearer_token: str | None = None
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:].strip()

    if config.AUTH_REQUIRED:
        if not bearer_token:
            raise HTTPException(status_code=401, detail="Authentication required")
        verified = await key_verifier.verify(bearer_token)
        if verified is None:
            raise HTTPException(status_code=401, detail="Invalid or expired API key")
        agent_id = verified["agent_id"]
        agent_role = verified["agent_role"]
        user_api_key = bearer_token  # Will be forwarded to MCP servers
        log.info("Authenticated: agent_id=%s, agent_role=%s", agent_id, agent_role)
    else:
        # Legacy mode: no auth, hardcoded developer role
        # Still check if bearer looks like a provider key for backward compat
        if bearer_token and len(bearer_token) > 10 and ("-" in bearer_token or bearer_token.startswith("sk")):
            user_api_key = bearer_token
```

Note: `user_api_key` is no longer used for LLM provider routing (central keys only). It is now the user's `kb_` key for MCP identity propagation. Remove the `user_api_key` parameter from `_resolve_provider_key()` calls and update that function to not accept it.

Update `_resolve_provider_key()` (lines 232-274): remove the `user_api_key` parameter and the `if user_api_key` branch for aliases:

```python
def _resolve_provider_key(model: str) -> tuple[Any, dict[str, Any]]:
    """Determine which acompletion callable + extra kwargs to use.

    For known aliases: use Router.
    For provider/model format: use direct litellm.acompletion with resolved key.
    Returns (acompletion_callable, extra_kwargs).
    Raises HTTPException if model can't be routed.
    """
    extra_kwargs: dict[str, Any] = {}

    if model in known_aliases:
        acompletion = router_acompletion or direct_acompletion
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

    # Resolve API key: provider env var → reject
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

Update the call site (around line 381):
```python
    acompletion, routing_kwargs = _resolve_provider_key(model=request.model)
```

**Step 4: Run tests**

```bash
cd pb-proxy && python -m pytest tests/ -v
```
Expected: All tests pass

**Step 5: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/tests/test_proxy_auth.py
git commit -m "feat(proxy): integrate API-key authentication into chat endpoint"
```

---

### Task 4: MCP server config model + YAML loading

**Files:**
- Create: `pb-proxy/mcp_servers.yaml`
- Create: `pb-proxy/mcp_config.py`
- Create: `pb-proxy/tests/test_mcp_config.py`

**Step 1: Write the failing test**

Create `pb-proxy/tests/test_mcp_config.py`:

```python
"""Tests for MCP server configuration loading."""

import pytest
import tempfile
import os
from pathlib import Path


def test_load_config_from_yaml():
    """Load MCP server config from YAML file."""
    from mcp_config import load_mcp_servers, McpServerConfig

    yaml_content = """
servers:
  - name: powerbrain
    url: http://mcp-server:8080/mcp
    auth: bearer
    prefix: powerbrain
    required: true
  - name: github
    url: http://github-mcp:3000/mcp
    auth: static
    auth_token_env: GITHUB_MCP_TOKEN
    prefix: github
    required: false
    tool_whitelist:
      - list_repos
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        servers = load_mcp_servers(f.name)

    os.unlink(f.name)

    assert len(servers) == 2
    assert servers[0].name == "powerbrain"
    assert servers[0].auth == "bearer"
    assert servers[0].required is True
    assert servers[0].tool_whitelist is None

    assert servers[1].name == "github"
    assert servers[1].auth == "static"
    assert servers[1].auth_token_env == "GITHUB_MCP_TOKEN"
    assert servers[1].required is False
    assert servers[1].tool_whitelist == ["list_repos"]


def test_fallback_to_env_var():
    """When no YAML exists, fall back to MCP_SERVER_URL env var."""
    from mcp_config import load_mcp_servers

    servers = load_mcp_servers("/nonexistent/path.yaml")

    assert len(servers) == 1
    assert servers[0].name == "powerbrain"
    assert servers[0].auth == "bearer"
    assert servers[0].required is True


def test_duplicate_prefix_raises():
    """Duplicate prefixes in config raise ValueError."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: server1
    url: http://s1:8080/mcp
    prefix: same
  - name: server2
    url: http://s2:8080/mcp
    prefix: same
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        with pytest.raises(ValueError, match="Duplicate prefix"):
            load_mcp_servers(f.name)
    os.unlink(f.name)


def test_duplicate_name_raises():
    """Duplicate server names in config raise ValueError."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: same
    url: http://s1:8080/mcp
    prefix: prefix1
  - name: same
    url: http://s2:8080/mcp
    prefix: prefix2
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        with pytest.raises(ValueError, match="Duplicate server name"):
            load_mcp_servers(f.name)
    os.unlink(f.name)
```

**Step 2: Run test to verify it fails**

```bash
cd pb-proxy && python -m pytest tests/test_mcp_config.py -v
```
Expected: FAIL (ImportError)

**Step 3: Implement mcp_config.py**

Create `pb-proxy/mcp_config.py`:

```python
"""
MCP server configuration: loads server definitions from YAML
with fallback to legacy MCP_SERVER_URL env var.
"""

import logging
import os
from dataclasses import dataclass, field

import yaml

import config

log = logging.getLogger("pb-proxy.mcp-config")


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server."""
    name: str
    url: str
    auth: str = "none"              # "bearer", "static", "none"
    auth_token_env: str | None = None  # env var name for static auth
    prefix: str = ""                # tool name prefix
    required: bool = False          # fail-fast if unreachable
    tool_whitelist: list[str] | None = None  # None = all tools


def load_mcp_servers(config_path: str) -> list[McpServerConfig]:
    """Load MCP server config from YAML or fall back to env var.

    Fallback: If the YAML file doesn't exist, creates a single-server
    config from the legacy MCP_SERVER_URL env var.
    """
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.info(
            "MCP servers config not found at %s, falling back to MCP_SERVER_URL",
            config_path,
        )
        return [
            McpServerConfig(
                name="powerbrain",
                url=config.MCP_SERVER_URL,
                auth="bearer",
                prefix="powerbrain",
                required=True,
            )
        ]

    servers_data = data.get("servers", [])
    if not servers_data:
        raise ValueError("MCP servers config has empty 'servers' list")

    servers = []
    for entry in servers_data:
        servers.append(McpServerConfig(
            name=entry["name"],
            url=entry["url"],
            auth=entry.get("auth", "none"),
            auth_token_env=entry.get("auth_token_env"),
            prefix=entry.get("prefix", entry["name"]),
            required=entry.get("required", False),
            tool_whitelist=entry.get("tool_whitelist"),
        ))

    # Validate: no duplicate names or prefixes
    names = [s.name for s in servers]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate server name in MCP config: {names}")
    prefixes = [s.prefix for s in servers]
    if len(prefixes) != len(set(prefixes)):
        raise ValueError(f"Duplicate prefix in MCP config: {prefixes}")

    log.info("Loaded %d MCP server(s): %s", len(servers), [s.name for s in servers])
    return servers
```

**Step 4: Create default mcp_servers.yaml**

Create `pb-proxy/mcp_servers.yaml`:

```yaml
# MCP server configuration for the Powerbrain AI Provider Proxy.
# Each server provides tools that are aggregated and injected into LLM requests.
#
# Auth modes:
#   bearer  — forwards the user's kb_ API key as Bearer token
#   static  — uses a fixed token from the env var specified in auth_token_env
#   none    — no Authorization header

servers:
  - name: powerbrain
    url: http://mcp-server:8080/mcp
    auth: bearer
    prefix: powerbrain
    required: true
```

**Step 5: Run tests**

```bash
cd pb-proxy && python -m pytest tests/test_mcp_config.py -v
```
Expected: All 4 tests PASS

**Step 6: Commit**

```bash
git add pb-proxy/mcp_config.py pb-proxy/mcp_servers.yaml pb-proxy/tests/test_mcp_config.py
git commit -m "feat(proxy): add MCP server config model with YAML loading and env var fallback"
```

---

### Task 5: Refactor ToolInjector for multi-server support

**Files:**
- Modify: `pb-proxy/tool_injection.py:1-161` (major refactor)
- Create: `pb-proxy/tests/test_tool_injection.py`

**Step 1: Write the failing test**

Create `pb-proxy/tests/test_tool_injection.py`:

```python
"""Tests for multi-server ToolInjector."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from mcp_config import McpServerConfig


@dataclass
class FakeTool:
    name: str
    description: str = "A test tool"
    inputSchema: dict | None = None


@pytest.fixture
def two_server_config():
    """Two MCP server configs."""
    return [
        McpServerConfig(
            name="powerbrain", url="http://mcp:8080/mcp",
            auth="bearer", prefix="powerbrain", required=True,
        ),
        McpServerConfig(
            name="github", url="http://github:3000/mcp",
            auth="static", auth_token_env="GITHUB_TOKEN",
            prefix="github", required=False,
        ),
    ]


def test_tool_entry_from_prefix():
    """ToolEntry stores server info and original name."""
    from tool_injection import ToolEntry

    entry = ToolEntry(
        server_name="powerbrain",
        original_name="search_knowledge",
        schema={"type": "function", "function": {"name": "powerbrain_search_knowledge"}},
        server_config=McpServerConfig(
            name="powerbrain", url="http://mcp:8080/mcp",
            auth="bearer", prefix="powerbrain", required=True,
        ),
    )
    assert entry.server_name == "powerbrain"
    assert entry.original_name == "search_knowledge"


def test_resolve_tool_name_with_prefix():
    """resolve_tool_name strips prefix and returns server + original name."""
    from tool_injection import ToolInjector, ToolEntry

    injector = ToolInjector.__new__(ToolInjector)
    injector._tools = {
        "powerbrain_search_knowledge": ToolEntry(
            server_name="powerbrain",
            original_name="search_knowledge",
            schema={},
            server_config=McpServerConfig(
                name="powerbrain", url="http://mcp:8080/mcp",
                auth="bearer", prefix="powerbrain", required=True,
            ),
        ),
        "github_list_repos": ToolEntry(
            server_name="github",
            original_name="list_repos",
            schema={},
            server_config=McpServerConfig(
                name="github", url="http://github:3000/mcp",
                auth="static", prefix="github", required=False,
            ),
        ),
    }

    entry = injector.resolve_tool("powerbrain_search_knowledge")
    assert entry is not None
    assert entry.server_name == "powerbrain"
    assert entry.original_name == "search_knowledge"

    entry = injector.resolve_tool("github_list_repos")
    assert entry is not None
    assert entry.server_name == "github"

    entry = injector.resolve_tool("unknown_tool")
    assert entry is None


def test_merge_tools_includes_all_servers():
    """merge_tools includes tools from all servers with prefixed names."""
    from tool_injection import ToolInjector, ToolEntry

    injector = ToolInjector.__new__(ToolInjector)
    injector._tools = {
        "powerbrain_search": ToolEntry(
            server_name="powerbrain", original_name="search",
            schema={"type": "function", "function": {"name": "powerbrain_search", "description": "Search", "parameters": {}}},
            server_config=McpServerConfig(name="powerbrain", url="u", prefix="powerbrain"),
        ),
        "github_list": ToolEntry(
            server_name="github", original_name="list",
            schema={"type": "function", "function": {"name": "github_list", "description": "List", "parameters": {}}},
            server_config=McpServerConfig(name="github", url="u", prefix="github"),
        ),
    }

    merged = injector.merge_tools(None)
    names = {t["function"]["name"] for t in merged}
    assert "powerbrain_search" in names
    assert "github_list" in names


def test_merge_tools_filters_by_allowed_servers():
    """merge_tools with allowed_servers filter only includes allowed tools."""
    from tool_injection import ToolInjector, ToolEntry

    injector = ToolInjector.__new__(ToolInjector)
    injector._tools = {
        "powerbrain_search": ToolEntry(
            server_name="powerbrain", original_name="search",
            schema={"type": "function", "function": {"name": "powerbrain_search", "description": "Search", "parameters": {}}},
            server_config=McpServerConfig(name="powerbrain", url="u", prefix="powerbrain"),
        ),
        "github_list": ToolEntry(
            server_name="github", original_name="list",
            schema={"type": "function", "function": {"name": "github_list", "description": "List", "parameters": {}}},
            server_config=McpServerConfig(name="github", url="u", prefix="github"),
        ),
    }

    merged = injector.merge_tools(None, allowed_servers=["powerbrain"])
    names = {t["function"]["name"] for t in merged}
    assert "powerbrain_search" in names
    assert "github_list" not in names
```

**Step 2: Run test to verify it fails**

```bash
cd pb-proxy && python -m pytest tests/test_tool_injection.py -v
```
Expected: FAIL (ToolEntry not defined)

**Step 3: Rewrite tool_injection.py**

Replace the entire `pb-proxy/tool_injection.py` with:

```python
"""
Multi-MCP-server tool injection: discovers tools from N MCP servers,
prefixes them with server name, and routes tool calls to the correct server.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import config
from mcp_config import McpServerConfig, load_mcp_servers

log = logging.getLogger("pb-proxy.tools")


@dataclass
class ToolEntry:
    """A discovered tool with routing metadata."""
    server_name: str
    original_name: str
    schema: dict              # OpenAI function-calling format
    server_config: McpServerConfig


def _mcp_headers(server: McpServerConfig, user_token: str | None = None) -> dict[str, str]:
    """Build auth headers for an MCP server connection.

    Args:
        server: The MCP server config.
        user_token: The user's kb_ API key (for bearer auth mode).
    """
    headers: dict[str, str] = {}
    if server.auth == "bearer":
        token = user_token or config.MCP_AUTH_TOKEN
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif server.auth == "static":
        if server.auth_token_env:
            token = os.getenv(server.auth_token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
    # auth == "none": no headers
    return headers


def _mcp_tool_to_openai(tool: Any, prefix: str) -> dict:
    """Convert an MCP Tool to OpenAI function-calling format with prefix."""
    prefixed_name = f"{prefix}_{tool.name}" if prefix else tool.name
    return {
        "type": "function",
        "function": {
            "name": prefixed_name,
            "description": tool.description or "",
            "parameters": tool.inputSchema or {"type": "object"},
        },
    }


class ToolInjector:
    """Discovers tools from multiple MCP servers and injects them into LLM requests."""

    def __init__(self) -> None:
        self._servers: list[McpServerConfig] = []
        self._tools: dict[str, ToolEntry] = {}   # prefixed_name -> ToolEntry
        self._refresh_task: asyncio.Task | None = None
        self._server_status: dict[str, bool] = {}  # server_name -> reachable

    async def start(self) -> None:
        """Load config, discover tools from all servers, start periodic refresh."""
        self._servers = load_mcp_servers(config.MCP_SERVERS_CONFIG)

        # Initial discovery
        await self._refresh_all_tools()

        # Check: all required servers must have tools
        for server in self._servers:
            if server.required and not self._server_status.get(server.name, False):
                raise RuntimeError(
                    f"Required MCP server '{server.name}' ({server.url}) is unreachable"
                )

        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        log.info(
            "ToolInjector started: %d server(s), %d tool(s)",
            len(self._servers), len(self._tools),
        )

    async def stop(self) -> None:
        """Stop periodic refresh."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _refresh_all_tools(self) -> None:
        """Refresh tools from all configured servers."""
        new_tools: dict[str, ToolEntry] = {}

        for server in self._servers:
            try:
                server_tools = await self._discover_server_tools(server)
                for prefixed_name, entry in server_tools.items():
                    new_tools[prefixed_name] = entry
                self._server_status[server.name] = True
                log.debug(
                    "Server '%s': %d tool(s)", server.name, len(server_tools),
                )
            except Exception as e:
                self._server_status[server.name] = False
                if server.required:
                    # For required servers, only raise on initial start
                    # (checked in start()). During refresh, keep cached tools.
                    log.error("Required server '%s' unreachable: %s", server.name, e)
                else:
                    log.warning("Optional server '%s' unreachable: %s", server.name, e)

        if new_tools:
            self._tools = new_tools
        elif self._tools:
            log.warning("All servers unreachable, keeping cached tools (%d)", len(self._tools))
        else:
            raise RuntimeError("No MCP servers reachable and no cached tools")

    async def _discover_server_tools(
        self, server: McpServerConfig,
    ) -> dict[str, ToolEntry]:
        """Connect to one MCP server and discover its tools."""
        headers = _mcp_headers(server)
        tools: dict[str, ToolEntry] = {}

        async with streamablehttp_client(server.url, headers=headers) as (
            read_stream, write_stream, _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()

                for tool in result.tools:
                    # Apply whitelist filter
                    if server.tool_whitelist and tool.name not in server.tool_whitelist:
                        continue
                    prefixed_name = f"{server.prefix}_{tool.name}" if server.prefix else tool.name
                    tools[prefixed_name] = ToolEntry(
                        server_name=server.name,
                        original_name=tool.name,
                        schema=_mcp_tool_to_openai(tool, server.prefix),
                        server_config=server,
                    )

        return tools

    async def _periodic_refresh(self) -> None:
        """Periodically refresh tool list from all servers."""
        while True:
            await asyncio.sleep(config.TOOL_REFRESH_INTERVAL)
            try:
                await self._refresh_all_tools()
            except Exception as e:
                log.warning("Periodic tool refresh failed: %s", e)

    def merge_tools(
        self,
        client_tools: list[dict] | None,
        allowed_servers: list[str] | None = None,
    ) -> list[dict]:
        """Merge MCP tools into client's tool array.

        Args:
            client_tools: Client-provided tools (preserved if unique names).
            allowed_servers: If set, only include tools from these servers.
                             If None, include all.
        """
        result: dict[str, dict] = {}

        # Add MCP tools (filtered by allowed_servers)
        for name, entry in self._tools.items():
            if allowed_servers is not None and entry.server_name not in allowed_servers:
                continue
            result[name] = entry.schema

        # Add client tools (skip conflicts — MCP tools win)
        if client_tools:
            for tool in client_tools:
                name = tool.get("function", {}).get("name", "")
                if name and name not in result:
                    result[name] = tool

        return list(result.values())

    @property
    def tool_names(self) -> set[str]:
        """Names of all currently known tools (prefixed)."""
        return set(self._tools.keys())

    @property
    def server_names(self) -> list[str]:
        """Names of all configured servers."""
        return [s.name for s in self._servers]

    def resolve_tool(self, name: str) -> ToolEntry | None:
        """Look up a tool by its prefixed name. Returns ToolEntry or None."""
        return self._tools.get(name)

    async def call_tool(
        self,
        entry: ToolEntry,
        arguments: dict,
        user_token: str | None = None,
    ) -> str:
        """Execute a tool call against the correct MCP server.

        Args:
            entry: The ToolEntry (from resolve_tool).
            arguments: Tool arguments dict.
            user_token: User's kb_ key for bearer auth propagation.
        """
        headers = _mcp_headers(entry.server_config, user_token=user_token)

        async with streamablehttp_client(
            entry.server_config.url, headers=headers,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(entry.original_name, arguments)
                texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        texts.append(content.text)
                return "\n".join(texts) if texts else str(result.content)
```

**Step 4: Run tests**

```bash
cd pb-proxy && python -m pytest tests/test_tool_injection.py -v
```
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add pb-proxy/tool_injection.py pb-proxy/tests/test_tool_injection.py
git commit -m "refactor(proxy): multi-server ToolInjector with prefix routing and per-server auth"
```

---

### Task 6: Update AgentLoop for server-aware tool routing

**Files:**
- Modify: `pb-proxy/agent_loop.py:1-159`

**Step 1: Write the failing test**

Create `pb-proxy/tests/test_agent_loop.py`:

```python
"""Tests for server-aware agent loop."""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass

from mcp_config import McpServerConfig
from tool_injection import ToolEntry


@dataclass
class FakeToolCall:
    id: str
    function: MagicMock


@dataclass
class FakeChoice:
    message: MagicMock


@dataclass
class FakeResponse:
    choices: list


@pytest.mark.asyncio
async def test_agent_loop_routes_to_correct_server():
    """Tool calls are routed to the correct MCP server via ToolEntry."""
    from agent_loop import AgentLoop

    # Setup mock injector
    injector = MagicMock()
    pb_entry = ToolEntry(
        server_name="powerbrain",
        original_name="search_knowledge",
        schema={},
        server_config=McpServerConfig(
            name="powerbrain", url="http://mcp:8080/mcp",
            auth="bearer", prefix="powerbrain", required=True,
        ),
    )
    injector.resolve_tool.return_value = pb_entry
    injector.call_tool = AsyncMock(return_value='{"results": []}')

    # Mock LLM: first call returns tool_call, second returns final response
    tool_call = FakeToolCall(
        id="call_1",
        function=MagicMock(
            name="powerbrain_search_knowledge",
            arguments='{"query": "test"}',
        ),
    )
    llm_call_1 = FakeResponse(choices=[FakeChoice(
        message=MagicMock(content=None, tool_calls=[tool_call])
    )])
    llm_call_2 = FakeResponse(choices=[FakeChoice(
        message=MagicMock(content="Here are the results", tool_calls=None)
    )])
    mock_acompletion = AsyncMock(side_effect=[llm_call_1, llm_call_2])

    loop = AgentLoop(
        injector,
        acompletion=mock_acompletion,
        max_iterations=5,
        user_token="kb_test_token_123",
    )
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "search for test"}],
        tools=[],
    )

    # Verify tool was called with entry and user_token
    injector.call_tool.assert_called_once_with(
        pb_entry, {"query": "test"}, user_token="kb_test_token_123",
    )
    assert result.tool_calls_executed == 1
    assert result.tools_used == ["search_knowledge"]
```

**Step 2: Run test to verify it fails**

```bash
cd pb-proxy && python -m pytest tests/test_agent_loop.py -v
```
Expected: FAIL (AgentLoop doesn't accept `user_token`)

**Step 3: Update agent_loop.py**

Replace the entire `pb-proxy/agent_loop.py`:

```python
"""
Agent loop: executes tool calls from LLM responses against
MCP servers (routed via ToolInjector) and re-submits results
until the LLM produces a final response.
"""

import json
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from tool_injection import ToolInjector
from pii_middleware import depseudonymize_tool_arguments
import config

log = logging.getLogger("pb-proxy.loop")

# Type alias for the async completion callable
ACompletion = Callable[..., Any]


@dataclass
class AgentLoopResult:
    """Result of an agent loop execution."""
    response: object                        # Final LiteLLM response
    iterations: int = 0                     # Number of LLM calls made
    tool_calls_executed: int = 0            # Total tool calls executed
    max_iterations_reached: bool = False    # True if loop was cut short
    tools_used: list[str] = field(default_factory=list)


class AgentLoop:
    """Executes the tool-call loop between LLM and MCP servers."""

    def __init__(
        self,
        tool_injector: ToolInjector,
        *,
        acompletion: ACompletion,
        max_iterations: int = 10,
        tool_call_timeout: int | None = None,
        pii_reverse_map: dict[str, str] | None = None,
        user_token: str | None = None,
    ) -> None:
        self._injector = tool_injector
        self._acompletion = acompletion
        self._max_iterations = max_iterations
        self._tool_call_timeout = tool_call_timeout or config.TOOL_CALL_TIMEOUT
        self._pii_reverse_map = pii_reverse_map or {}
        self._user_token = user_token

    async def run(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        **litellm_kwargs,
    ) -> AgentLoopResult:
        """Run the agent loop until final response or max iterations."""
        result = AgentLoopResult(response=None)
        working_messages = list(messages)

        for iteration in range(1, self._max_iterations + 1):
            result.iterations = iteration

            # Call LLM
            response = await self._acompletion(
                model=model,
                messages=working_messages,
                tools=tools if tools else None,
                **litellm_kwargs,
            )

            choice = response.choices[0]

            # No tool calls → final response
            if not choice.message.tool_calls:
                result.response = response
                return result

            # Append assistant message with tool calls
            assistant_msg = {
                "role": "assistant",
                "content": choice.message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in choice.message.tool_calls
                ],
            }
            working_messages.append(assistant_msg)

            # Execute each tool call
            for tc in choice.message.tool_calls:
                tool_name = tc.function.name
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON in tool arguments for %s: %s",
                                tool_name, tc.function.arguments)
                    arguments = {}

                # De-pseudonymize tool arguments before MCP call
                if self._pii_reverse_map:
                    arguments = depseudonymize_tool_arguments(
                        arguments, self._pii_reverse_map
                    )

                # Resolve tool: look up ToolEntry by prefixed name
                entry = self._injector.resolve_tool(tool_name)

                if entry:
                    log.info("Routing tool '%s' → server '%s', tool '%s'",
                             tool_name, entry.server_name, entry.original_name)
                    try:
                        tool_result = await asyncio.wait_for(
                            self._injector.call_tool(
                                entry, arguments,
                                user_token=self._user_token,
                            ),
                            timeout=self._tool_call_timeout,
                        )
                        result.tool_calls_executed += 1
                        result.tools_used.append(entry.original_name)
                    except asyncio.TimeoutError:
                        tool_result = json.dumps({
                            "error": f"Tool '{entry.original_name}' on server "
                                     f"'{entry.server_name}' timed out after "
                                     f"{self._tool_call_timeout}s"
                        })
                        log.warning("Tool call timed out: %s/%s",
                                    entry.server_name, entry.original_name)
                    except Exception as e:
                        tool_result = json.dumps({
                            "error": f"Tool '{entry.original_name}' on server "
                                     f"'{entry.server_name}' failed: {str(e)}"
                        })
                        log.error("Tool call failed: %s/%s — %s",
                                  entry.server_name, entry.original_name, e)
                else:
                    # Unknown tool
                    tool_result = json.dumps({
                        "error": f"Unknown tool '{tool_name}'. "
                                 f"Available tools: {sorted(self._injector.tool_names)}"
                    })
                    log.warning("Unknown tool requested: %s", tool_name)

                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

        # Max iterations reached
        result.max_iterations_reached = True
        result.response = response
        log.warning("Agent loop reached max iterations (%d)", self._max_iterations)
        return result
```

**Step 4: Run tests**

```bash
cd pb-proxy && python -m pytest tests/test_agent_loop.py -v
```
Expected: PASS

**Step 5: Commit**

```bash
git add pb-proxy/agent_loop.py pb-proxy/tests/test_agent_loop.py
git commit -m "refactor(proxy): server-aware agent loop with user_token propagation"
```

---

### Task 7: Update proxy.py for multi-server + auth integration

**Files:**
- Modify: `pb-proxy/proxy.py` (update OPA call, tool merge, agent loop construction)

**Step 1: Update OPA helper to include configured_servers**

In `proxy.py`, update `check_opa_policy()` to pass `configured_servers`:

```python
async def check_opa_policy(
    agent_role: str, provider: str, configured_servers: list[str],
) -> dict:
    """Check proxy policies via OPA."""
    if http_client is None:
        raise RuntimeError("http_client not initialized (lifespan not started)")
    opa_input = {
        "input": {
            "agent_role": agent_role,
            "provider": provider,
            "configured_servers": configured_servers,
        }
    }
    try:
        resp = await http_client.post(
            f"{config.OPA_URL}/v1/data/kb/proxy",
            json=opa_input,
        )
        resp.raise_for_status()
        return resp.json().get("result", {})
    except Exception as e:
        log.error("OPA policy check failed: %s", e)
        return {"provider_allowed": False, "max_iterations": 0}
```

**Step 2: Update OPA call site**

In `chat_completions()`, update the OPA call (around line 299):

```python
    policy = await check_opa_policy(
        agent_role, request.model, tool_injector.server_names,
    )
```

**Step 3: Update tool merge to filter by allowed servers**

After OPA call:
```python
    allowed_servers = policy.get("mcp_servers_allowed", tool_injector.server_names)
```

Update tool injection section:
```python
    if config.TOOL_INJECTION_ENABLED:
        merged_tools = tool_injector.merge_tools(
            request.tools, allowed_servers=allowed_servers,
        )
    else:
        merged_tools = request.tools or []
```

**Step 4: Update AgentLoop construction**

Update the AgentLoop construction to pass `user_token`:

```python
    loop = AgentLoop(
        tool_injector,
        acompletion=acompletion,
        max_iterations=max_iterations,
        pii_reverse_map=pii_reverse_map,
        user_token=user_api_key,
    )
```

**Step 5: Run all tests**

```bash
cd pb-proxy && python -m pytest tests/ -v
```
Expected: All tests pass

**Step 6: Commit**

```bash
git add pb-proxy/proxy.py
git commit -m "feat(proxy): wire multi-server tool injection + auth into chat endpoint"
```

---

### Task 8: Add OPA `mcp_servers_allowed` policy

**Files:**
- Modify: `opa-policies/kb/proxy.rego:72` (add new rule)
- Modify: `opa-policies/kb/test_proxy.rego:118` (add tests)

**Step 1: Write the failing OPA test**

Add to `opa-policies/kb/test_proxy.rego`:

```rego
# ── MCP Servers Allowed ──────────────────────────────────────

test_mcp_servers_default_powerbrain if {
    proxy.mcp_servers_allowed == ["powerbrain"] with input as {
        "agent_role": "analyst",
        "configured_servers": ["powerbrain", "github"],
    }
}

test_mcp_servers_developer_all if {
    proxy.mcp_servers_allowed == ["powerbrain", "github"] with input as {
        "agent_role": "developer",
        "configured_servers": ["powerbrain", "github"],
    }
}

test_mcp_servers_admin_all if {
    proxy.mcp_servers_allowed == ["powerbrain", "github", "tools"] with input as {
        "agent_role": "admin",
        "configured_servers": ["powerbrain", "github", "tools"],
    }
}
```

**Step 2: Run OPA tests to verify failure**

```bash
docker exec kb-opa /opa test /policies/kb/ -v
```
Expected: New tests fail (`mcp_servers_allowed` not defined)

**Step 3: Add the policy rule**

Add to `opa-policies/kb/proxy.rego` after line 72:

```rego
# ── MCP Server Access ────────────────────────────────────────
# Controls which MCP servers each role may access.
# Default: only powerbrain. Developer and admin: all configured servers.

default mcp_servers_allowed := ["powerbrain"]

mcp_servers_allowed := input.configured_servers if {
    input.agent_role in {"developer", "admin"}
}
```

**Step 4: Run OPA tests**

```bash
docker exec kb-opa /opa test /policies/kb/ -v
```
Expected: All tests pass (21 total: 18 existing + 3 new)

**Step 5: Commit**

```bash
git add opa-policies/kb/proxy.rego opa-policies/kb/test_proxy.rego
git commit -m "feat(opa): add mcp_servers_allowed policy for per-role server access control"
```

---

### Task 9: Update docker-compose.yml and documentation

**Files:**
- Modify: `docker-compose.yml:337-384`
- Modify: `CLAUDE.md`

**Step 1: Final docker-compose updates**

Ensure the pb-proxy service in docker-compose.yml has all new env vars, volumes, and depends_on (as specified in Task 1). Also remove the now-unused `MCP_AUTH_TOKEN` env var since auth is propagated per-user.

Actually, keep `MCP_AUTH_TOKEN` as a fallback for when `AUTH_REQUIRED=false` (legacy mode). The `_mcp_headers` function in `tool_injection.py` already uses it as fallback when `user_token` is None.

**Step 2: Update CLAUDE.md**

Update the MCP Tools count from 11 to 11 (unchanged), update the proxy section to document:
- Auth is now required (API key in Bearer header)
- Multi-MCP-server support via `mcp_servers.yaml`
- OPA `mcp_servers_allowed` policy

Add to Completed Features:
```
16. ✅ **Proxy Authentication** — API-key auth for proxy, identity propagation to MCP servers
17. ✅ **Multi-MCP-Server Aggregation** — Proxy aggregates tools from N MCP servers with per-server auth, prefix namespacing, and OPA-controlled access
```

**Step 3: Commit**

```bash
git add docker-compose.yml CLAUDE.md
git commit -m "docs: update CLAUDE.md and docker-compose for proxy auth + multi-MCP"
```

---

### Task 10: End-to-end verification

**Step 1: Run all Python tests**

```bash
cd pb-proxy && python -m pytest tests/ -v
```

**Step 2: Run OPA tests**

```bash
docker exec kb-opa /opa test /policies/kb/ -v
```

**Step 3: Verify Docker build**

```bash
docker compose --profile proxy build pb-proxy
```

**Step 4: Manual smoke test (if Docker is running)**

```bash
# Create a test API key
docker exec kb-mcp python manage_keys.py create --agent-id test-proxy --role developer

# Test authenticated request
curl -s http://localhost:8090/v1/chat/completions \
  -H "Authorization: Bearer kb_<key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}'

# Test unauthenticated request (should be 401)
curl -s http://localhost:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}'
```

**Step 5: Final commit if any fixes needed**

```bash
git add -A && git commit -m "fix: address issues found during E2E verification"
```
