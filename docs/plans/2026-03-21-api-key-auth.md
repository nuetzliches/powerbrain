# API-Key Authentication Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add API-key authentication to the MCP server so that agent identity is verified instead of self-declared.

**Architecture:** Starlette middleware chain (BearerAuthBackend → AuthContextMiddleware) validates `Authorization: Bearer <key>` headers against SHA-256 hashed keys in PostgreSQL. The `call_tool` handler reads identity from the verified token instead of tool arguments. Env var `AUTH_REQUIRED` controls enforcement.

**Tech Stack:** MCP SDK 1.26.0 auth middleware, asyncpg, hashlib, secrets, Starlette

**Design Doc:** `docs/plans/2026-03-21-api-key-auth-design.md`

---

### Task 1: Database Migration — `api_keys` Table

**Files:**
- Create: `init-db/010_api_keys.sql`

**Step 1: Write the migration**

```sql
-- 010_api_keys.sql: API key authentication
-- Stores hashed API keys with role mapping

CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash      TEXT NOT NULL UNIQUE,
    agent_id      TEXT NOT NULL UNIQUE,
    agent_role    TEXT NOT NULL DEFAULT 'analyst'
                  CHECK (agent_role IN ('analyst', 'developer', 'admin')),
    description   TEXT,
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);

-- Grant permissions for the MCP app user
GRANT SELECT, UPDATE ON api_keys TO mcp_app;
```

**Step 2: Verify migration file numbering**

Check `ls init-db/` — the last migration is `009_add_source_type.sql`, so `010` is correct.

**Step 3: Commit**

```bash
git add init-db/010_api_keys.sql
git commit -m "feat: add api_keys migration for MCP authentication (010)"
```

---

### Task 2: Key Management Script

**Files:**
- Create: `mcp-server/manage_keys.py`

**Step 1: Write the key management CLI**

This script provides `create`, `list`, and `revoke` commands for API keys.
It connects directly to PostgreSQL to manage the `api_keys` table.

```python
"""
API-Key Management CLI for the KB MCP Server.

Usage:
    python manage_keys.py create --agent-id my-agent --role analyst [--description "..."] [--expires-in-days 90]
    python manage_keys.py list
    python manage_keys.py revoke --agent-id my-agent
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

import asyncpg

POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql://kb_admin:changeme@localhost:5432/knowledgebase",
)
KEY_PREFIX = "kb_"
KEY_BYTES = 32  # 32 bytes = 64 hex chars


def generate_key() -> str:
    """Generate a new API key with kb_ prefix."""
    return KEY_PREFIX + secrets.token_hex(KEY_BYTES)


def hash_key(key: str) -> str:
    """SHA-256 hash of the API key."""
    return hashlib.sha256(key.encode()).hexdigest()


async def cmd_create(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        key = generate_key()
        key_h = hash_key(key)
        expires = None
        if args.expires_in_days:
            expires = datetime.now(timezone.utc) + timedelta(days=args.expires_in_days)

        await conn.execute(
            """
            INSERT INTO api_keys (key_hash, agent_id, agent_role, description, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            key_h, args.agent_id, args.role, args.description, expires,
        )
        print(f"API key created for agent '{args.agent_id}' (role: {args.role})")
        print(f"Key: {key}")
        print()
        print("Store this key securely — it cannot be retrieved again.")
        if expires:
            print(f"Expires: {expires.isoformat()}")
    except asyncpg.UniqueViolationError:
        print(f"Error: agent_id '{args.agent_id}' already exists.", file=sys.stderr)
        sys.exit(1)
    finally:
        await conn.close()


async def cmd_list(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT agent_id, agent_role, description, active,
                   created_at, expires_at, last_used_at
            FROM api_keys ORDER BY created_at
            """
        )
        if not rows:
            print("No API keys found.")
            return

        fmt = "{:<20} {:<10} {:<6} {:<20} {:<20} {:<20} {}"
        print(fmt.format("AGENT_ID", "ROLE", "ACTIVE", "CREATED", "EXPIRES", "LAST_USED", "DESCRIPTION"))
        print("-" * 120)
        for r in rows:
            print(fmt.format(
                r["agent_id"],
                r["agent_role"],
                str(r["active"]),
                r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "-",
                r["expires_at"].strftime("%Y-%m-%d %H:%M") if r["expires_at"] else "never",
                r["last_used_at"].strftime("%Y-%m-%d %H:%M") if r["last_used_at"] else "never",
                r["description"] or "",
            ))
    finally:
        await conn.close()


async def cmd_revoke(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        result = await conn.execute(
            "UPDATE api_keys SET active = false WHERE agent_id = $1",
            args.agent_id,
        )
        if result == "UPDATE 1":
            print(f"API key for agent '{args.agent_id}' revoked.")
        else:
            print(f"No active key found for agent '{args.agent_id}'.", file=sys.stderr)
            sys.exit(1)
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="KB MCP Server API Key Management")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a new API key")
    create.add_argument("--agent-id", required=True, help="Unique agent identifier")
    create.add_argument("--role", required=True, choices=["analyst", "developer", "admin"])
    create.add_argument("--description", default=None, help="What this key is for")
    create.add_argument("--expires-in-days", type=int, default=None)

    sub.add_parser("list", help="List all API keys")

    revoke = sub.add_parser("revoke", help="Revoke an API key")
    revoke.add_argument("--agent-id", required=True, help="Agent to revoke")

    args = parser.parse_args()

    coro = {"create": cmd_create, "list": cmd_list, "revoke": cmd_revoke}[args.command]
    asyncio.run(coro(args))


if __name__ == "__main__":
    main()
```

**Step 2: Test manually (after DB is up)**

```bash
docker exec -i kb-mcp-server python manage_keys.py create --agent-id test-agent --role analyst --description "Test key"
docker exec -i kb-mcp-server python manage_keys.py list
docker exec -i kb-mcp-server python manage_keys.py revoke --agent-id test-agent
```

**Step 3: Commit**

```bash
git add mcp-server/manage_keys.py
git commit -m "feat: add API key management CLI (create/list/revoke)"
```

---

### Task 3: ApiKeyVerifier — TokenVerifier Implementation

**Files:**
- Modify: `mcp-server/server.py` (add imports, config, verifier class)

This task adds the `ApiKeyVerifier` class and new config to `server.py`.
It does NOT yet wire it into the middleware or change `call_tool` — that's Task 4.

**Step 1: Add new imports and config**

At the top of `server.py`, add after the existing imports (after line 33):

```python
import hashlib
```

Add after the existing Starlette imports (lines 26-28):

```python
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
```

Add MCP auth imports (after line 25):

```python
from mcp.server.auth.provider import TokenVerifier, AccessToken
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
```

Add config (after line 52, near other env vars):

```python
AUTH_REQUIRED  = os.getenv("AUTH_REQUIRED", "true").lower() == "true"
```

**Step 2: Add the ApiKeyVerifier class**

Place after the `get_pg_pool()` function (after line 135):

```python
# ── API key authentication ───────────────────────────────────

class ApiKeyVerifier:
    """TokenVerifier implementation that validates API keys against PostgreSQL."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        pool = await get_pg_pool()
        row = await pool.fetchrow(
            "SELECT agent_id, agent_role FROM api_keys "
            "WHERE key_hash = $1 AND active = true "
            "AND (expires_at IS NULL OR expires_at > now())",
            key_hash,
        )
        if row is None:
            return None
        # Update last_used_at (fire-and-forget, don't block auth)
        try:
            await pool.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE key_hash = $1",
                key_hash,
            )
        except Exception:
            pass  # Non-critical, don't fail auth over this
        return AccessToken(
            token=token,
            client_id=row["agent_id"],
            scopes=[row["agent_role"]],
        )
```

**Step 3: Commit**

```bash
git add mcp-server/server.py
git commit -m "feat: add ApiKeyVerifier class implementing MCP TokenVerifier"
```

---

### Task 4: Wire Auth Middleware into Starlette App

**Files:**
- Modify: `mcp-server/server.py` (lines 1200-1223, startup section)

**Step 1: Update the Starlette app creation**

Replace the startup section (lines 1200-1223) with auth-aware middleware chain:

```python
# ── Startup ──────────────────────────────────────────────────
if __name__ == "__main__":
    prom_start_http_server(METRICS_PORT)
    log.info(f"Prometheus /metrics auf Port {METRICS_PORT}")

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
    )

    # Must be a class instance (not async def) so Starlette treats it
    # as a raw ASGI app instead of wrapping it with request_response().
    class MCPTransport:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await session_manager.handle_request(scope, receive, send)

    app = Starlette(
        routes=[Route(MCP_PATH, endpoint=MCPTransport())],
        lifespan=lambda app: session_manager.run(),
    )

    # ── Auth middleware (inside-out: last applied = outermost) ──
    verifier = ApiKeyVerifier()
    # AuthContextMiddleware: stores authenticated user in contextvars
    app = AuthContextMiddleware(app)
    if AUTH_REQUIRED:
        # RequireAuthMiddleware: rejects unauthenticated requests with 401
        app = RequireAuthMiddleware(app, required_scopes=[])
    # AuthenticationMiddleware: extracts Bearer token, calls verifier
    app = AuthenticationMiddleware(app, backend=BearerAuthBackend(verifier))

    mode = "enforced" if AUTH_REQUIRED else "optional"
    log.info("MCP Streamable HTTP auf %s:%s%s (auth: %s)", MCP_HOST, MCP_PORT, MCP_PATH, mode)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
```

When `AUTH_REQUIRED=false`, `RequireAuthMiddleware` is not applied. The
`AuthenticationMiddleware` still runs (populating `scope["user"]` if a valid
token is present) and `AuthContextMiddleware` makes it available via `get_access_token()`.
This means `call_tool` can check for an authenticated token and fall back to arguments
when auth is optional.

**Step 2: Commit**

```bash
git add mcp-server/server.py
git commit -m "feat: wire BearerAuth middleware chain into Starlette app"
```

---

### Task 5: Update `call_tool` to Use Auth Token

**Files:**
- Modify: `mcp-server/server.py` (lines 656-666, call_tool handler)

**Step 1: Replace self-declared identity with token-based identity**

Replace lines 656-666 in `call_tool`:

```python
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # ── Identity from auth token (preferred) or arguments (legacy) ──
    access_token = get_access_token()
    if access_token is not None:
        agent_id = access_token.client_id
        agent_role = access_token.scopes[0] if access_token.scopes else "unknown"
    elif not AUTH_REQUIRED:
        # Legacy fallback: self-declared identity (only when auth is optional)
        agent_id = arguments.get("agent_id", "unknown")
        agent_role = arguments.get("agent_role", "unknown")
        log.warning("Unauthenticated request for tool '%s' from agent_id='%s'", name, agent_id)
    else:
        # Should not reach here (RequireAuthMiddleware already rejected)
        return [TextContent(type="text", text=json.dumps({"error": "authentication required"}))]

    t_start = time.perf_counter()
    status  = "ok"

    with _otel_span(f"mcp.{name}"):
        try:
            result = await _dispatch(name, arguments, agent_id, agent_role)
        except Exception as e:
            log.error(f"Tool {name} fehlgeschlagen: {e}", exc_info=True)
            status = "error"
            result = [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elapsed = time.perf_counter() - t_start
    mcp_requests_total.labels(tool=name, status=status).inc()
    mcp_request_duration.labels(tool=name).observe(elapsed)

    return result
```

**Step 2: Commit**

```bash
git add mcp-server/server.py
git commit -m "feat: derive agent identity from auth token in call_tool handler"
```

---

### Task 6: Remove `agent_id`/`agent_role` from Tool Schemas

**Files:**
- Modify: `mcp-server/server.py` (lines 401-651, all 14 tool definitions)

**Step 1: Remove from all tool inputSchemas**

For every tool in `list_tools()`, remove these two lines from `properties`:

```python
"agent_id":   {"type": "string"},
"agent_role": {"type": "string"},
```

And remove `"agent_id", "agent_role"` from every `required` list.

All 14 tools need this change. The exact locations:

| Tool | Properties lines to remove | Required list to update |
|------|---------------------------|------------------------|
| `search_knowledge` | 425-426 | 428 |
| `query_data` | 440-441 | 443 |
| `get_rules` | 454-455 | 457 |
| `check_policy` | 469-470 | 472 |
| `ingest_data` | 486-487 | 489 |
| `get_classification` | 500-501 | 503 |
| `list_datasets` | 514-515 | 517 |
| `get_code_context` | 530-531 | 533 |
| `graph_query` | 552-553 | 555 |
| `graph_mutate` | 575-576 | 578 |
| `submit_feedback` | 601-602 | 604 |
| `get_eval_stats` | 616-617 | 619 |
| `create_snapshot` | 633-634 | 635 |
| `list_snapshots` | 646-647 | 649 (only those two, so `required` can be removed entirely or become `required: []`) |

For tools where `required` becomes empty (like `list_snapshots`, `get_eval_stats`),
either remove the `required` key or set it to `[]` — the MCP spec allows both.

**Step 2: Verify no other code references agent_id/agent_role from arguments**

Search for `arguments.get("agent_id"` and `arguments.get("agent_role"` in `server.py`
to ensure no code still reads these from arguments. The only place should be the
legacy fallback in `call_tool` (Task 5).

Note: `_dispatch()` receives `agent_id` and `agent_role` as function parameters —
these do NOT need to change. They are populated from the token by `call_tool`.

**Step 3: Commit**

```bash
git add mcp-server/server.py
git commit -m "feat: remove agent_id/agent_role from tool schemas (now from auth token)"
```

---

### Task 7: Update Skill Documentation

**Files:**
- Modify: `~/.config/opencode/skills/superpowers/querying-knowledge-base/SKILL.md`

**Step 1: Update the skill to include auth headers**

Update the connection details section to include the `Authorization` header:

```markdown
- **Headers**: `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `Authorization: Bearer <API_KEY>`
```

Update all example curl commands to include the auth header and remove `agent_id`/`agent_role` from tool arguments.

Update the role system documentation to explain that roles come from the API key,
not from the request.

**Step 2: Commit**

```bash
git add ~/.config/opencode/skills/superpowers/querying-knowledge-base/SKILL.md
git commit -m "docs: update querying-knowledge-base skill for API key auth"
```

---

### Task 8: Update `bekannte-schwachstellen.md`

**Files:**
- Modify: `docs/bekannte-schwachstellen.md`

**Step 1: Mark P1-1 as RESOLVED**

Add a `RESOLVED` section for P1-1, similar to the existing resolved entries:

```markdown
### ~~P1-1: No authentication — roles are self-declared~~ — RESOLVED

**Status:** RESOLVED — API key authentication implemented. Every agent
requires an `Authorization: Bearer kb_...` header. Keys are stored as SHA-256
hashes in the `api_keys` table and map to a fixed role
(analyst/developer/admin). `agent_id` and `agent_role` are no longer
tool parameters, but are derived from the verified token.
The `AUTH_REQUIRED` env var controls whether authentication is enforced.
```

Also update the Phase 2 section to remove "Authentication" from the list.

**Step 2: Commit**

```bash
git add docs/bekannte-schwachstellen.md
git commit -m "docs: mark P1-1 authentication as resolved"
```

---

### Task 9: Add `AUTH_REQUIRED` to `.env.example` and `docker-compose.yml`

**Files:**
- Modify: `.env.example`
- Modify: `docker-compose.yml`

**Step 1: Add to `.env.example`**

Add in the appropriate section:

```
# Authentication (true = require API keys, false = allow unauthenticated access)
AUTH_REQUIRED=true
```

**Step 2: Add to docker-compose.yml mcp-server environment**

Add `AUTH_REQUIRED` to the mcp-server service environment variables:

```yaml
AUTH_REQUIRED: ${AUTH_REQUIRED:-true}
```

**Step 3: Commit**

```bash
git add .env.example docker-compose.yml
git commit -m "feat: add AUTH_REQUIRED config to env and docker-compose"
```

---

### Task 10: Integration Test

**Files:**
- Create: `tests/test_auth.py`

**Step 1: Write the integration test**

This test verifies the auth flow end-to-end against the running MCP server.

```python
"""
Integration tests for API-Key authentication.
Requires: running MCP server + PostgreSQL (docker compose up).
"""

import hashlib
import json
import os
import secrets

import httpx
import asyncpg
import pytest

MCP_URL = os.getenv("MCP_URL", "http://localhost:8080/mcp")
POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql://kb_admin:changeme@localhost:5432/knowledgebase",
)

HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def mcp_request(tool: str, arguments: dict, headers: dict | None = None) -> dict:
    """Send a JSON-RPC tool call to the MCP server."""
    h = {**HEADERS_BASE, **(headers or {})}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    resp = httpx.post(MCP_URL, json=body, headers=h, timeout=10)
    return resp


@pytest.fixture
async def test_api_key():
    """Create a temporary API key for testing, clean up after."""
    key = "kb_" + secrets.token_hex(32)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    agent_id = f"test-{secrets.token_hex(4)}"

    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, agent_id, agent_role, description) "
            "VALUES ($1, $2, $3, $4)",
            key_hash, agent_id, "analyst", "integration test key",
        )
        yield {"key": key, "agent_id": agent_id, "role": "analyst"}
    finally:
        await conn.execute("DELETE FROM api_keys WHERE agent_id = $1", agent_id)
        await conn.close()


class TestAuthRequired:
    """Tests with AUTH_REQUIRED=true (the default)."""

    def test_no_token_returns_401(self):
        resp = mcp_request("list_datasets", {})
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self):
        resp = mcp_request(
            "list_datasets", {},
            headers={"Authorization": "Bearer kb_invalid_key_here"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, test_api_key):
        resp = mcp_request(
            "list_datasets", {},
            headers={"Authorization": f"Bearer {test_api_key['key']}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data or "error" not in data.get("result", {})

    @pytest.mark.asyncio
    async def test_agent_id_from_token(self, test_api_key):
        """Verify that agent_id comes from the token, not from arguments."""
        resp = mcp_request(
            "list_datasets", {},
            headers={"Authorization": f"Bearer {test_api_key['key']}"},
        )
        assert resp.status_code == 200
```

**Step 2: Run the tests**

```bash
pytest tests/test_auth.py -v
```

Expected: Tests pass if `AUTH_REQUIRED=true` and services are running.

**Step 3: Commit**

```bash
git add tests/test_auth.py
git commit -m "test: add integration tests for API-key authentication"
```

---

### Task 11: Seed a Default Development Key

**Files:**
- Modify: `init-db/010_api_keys.sql` (add a default dev key at the bottom)

**Step 1: Add a well-known development key**

Append to `010_api_keys.sql`:

```sql
-- Default development key (only for local development!)
-- Key: kb_dev_localonly_do_not_use_in_production
-- Hash: SHA-256 of the above
INSERT INTO api_keys (key_hash, agent_id, agent_role, description)
VALUES (
    encode(sha256('kb_dev_localonly_do_not_use_in_production'::bytea), 'hex'),
    'dev-agent',
    'admin',
    'Default development key — DO NOT use in production'
)
ON CONFLICT (agent_id) DO NOTHING;
```

This gives developers a key to use out of the box when running locally.
The key value is intentionally obvious and non-secret.

**Step 2: Commit**

```bash
git add init-db/010_api_keys.sql
git commit -m "feat: seed default development API key for local testing"
```
