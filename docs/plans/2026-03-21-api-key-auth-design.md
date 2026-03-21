# Design: API-Key Authentication for MCP Server

**Date:** 2026-03-21
**Status:** Approved
**Resolves:** P1-1 (Keine Authentifizierung — Rollen sind selbst-deklariert)

## Problem

The MCP server accepts `agent_id` and `agent_role` as self-declared tool arguments.
Any caller claiming `agent_role: "admin"` gets full access to all classification levels,
including `confidential` and `restricted`. OPA evaluates the claimed role without
verifying it. The entire authorization chain (OPA, audit log, vault access) depends
on unverified identity.

## Decision

Implement API-Key authentication using the MCP SDK's built-in `TokenVerifier` protocol
and Starlette middleware. Each API key maps to a fixed agent identity and role stored
in PostgreSQL. Agent identity is derived from the verified token, never from request
arguments.

## Architecture

```
Agent sends: Authorization: Bearer kb_a1b2c3d4e5...
                │
                ▼
┌───────────────────────────────────┐
│  BearerAuthBackend (MCP SDK)      │
│  → Extracts token from header     │
│  → Calls TokenVerifier.verify()   │
└───────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────┐
│  ApiKeyVerifier (our impl.)       │
│  → SHA-256(token)                 │
│  → SELECT FROM api_keys           │
│    WHERE key_hash = $1            │
│    AND active = true              │
│    AND (expires_at IS NULL        │
│         OR expires_at > now())    │
│  → UPDATE last_used_at            │
│  → Return AccessToken             │
│    (client_id=agent_id,           │
│     scopes=[agent_role])          │
└───────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────┐
│  AuthContextMiddleware (MCP SDK)  │
│  → Stores in ContextVar           │
│  → get_access_token() available   │
└───────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────┐
│  call_tool handler                │
│  → agent_id from token            │
│  → agent_role from token scopes   │
│  → NOT from arguments             │
└───────────────────────────────────┘
```

## Data Model

New migration `init-db/009_api_keys.sql`:

```sql
CREATE TABLE api_keys (
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

CREATE INDEX idx_api_keys_hash ON api_keys (key_hash);
```

Keys are stored as SHA-256 hashes only. The plaintext key is shown once at creation
time. Key format: `kb_` prefix + 32 bytes random hex (e.g. `kb_a1b2c3d4e5f6...`).

## Components

### ApiKeyVerifier (server.py)

Implements the MCP SDK `TokenVerifier` protocol:

```python
class ApiKeyVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        row = await pool.fetchrow(
            "SELECT agent_id, agent_role FROM api_keys "
            "WHERE key_hash = $1 AND active = true "
            "AND (expires_at IS NULL OR expires_at > now())",
            key_hash
        )
        if not row:
            return None
        await pool.execute(
            "UPDATE api_keys SET last_used_at = now() WHERE key_hash = $1",
            key_hash
        )
        return AccessToken(
            token=token,
            client_id=row["agent_id"],
            scopes=[row["agent_role"]],
        )
```

### Middleware Chain (server.py)

Added to the Starlette app:

```python
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware

middleware = [
    Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier)),
    Middleware(AuthContextMiddleware),
    Middleware(RequireAuthMiddleware),
]
app = Starlette(routes=[...], middleware=middleware, lifespan=...)
```

### call_tool Handler Changes

Before (insecure):
```python
agent_id = arguments.get("agent_id", "unknown")
agent_role = arguments.get("agent_role", "unknown")
```

After (from verified token):
```python
from mcp.server.auth.middleware.auth_context import get_access_token
token = get_access_token()
agent_id = token.client_id
agent_role = token.scopes[0]  # Role stored as first scope
```

### Tool Schema Changes

`agent_id` and `agent_role` are removed from all 14 tool `inputSchema` definitions.
They are no longer tool parameters — identity comes exclusively from the auth token.

### Key Management Script

`mcp-server/manage_keys.py` — CLI for key lifecycle:

- `create --agent-id <id> --role <role> [--description <desc>] [--expires-in-days <n>]`
  Generates key, prints plaintext once, stores hash in PG.
- `list` — Shows all keys (agent_id, role, active, created_at, expires_at, last_used_at).
- `revoke --agent-id <id>` — Sets `active = false`.

### Backward Compatibility

Environment variable `AUTH_REQUIRED` (default: `true`):

- `true` — All requests require `Authorization: Bearer <key>`. Unauthenticated
  requests receive 401.
- `false` — Unauthenticated requests are accepted with self-declared roles (legacy
  behavior). Authenticated requests always take precedence. Logs a warning on
  unauthenticated access.

This allows gradual rollout: deploy with `AUTH_REQUIRED=false`, create keys, update
agents, then switch to `AUTH_REQUIRED=true`.

### Skill Update

The `querying-knowledge-base` skill must be updated:
- Add `Authorization: Bearer <key>` header to all requests
- Remove `agent_id` and `agent_role` from tool call arguments
- Document that keys are configured per-agent

## What Does NOT Change

- **OPA policies** — `agent_role` is still passed to OPA, just sourced from verified
  auth instead of self-declared arguments
- **Audit logging** — Same `agent_id` and `agent_role` fields, now trustworthy
- **Qdrant** — No changes
- **Ingestion** — No changes (ingestion has its own service-to-service path)

## Security Considerations

- API keys are hashed with SHA-256 before storage (no plaintext in DB)
- Keys have optional expiration dates
- `last_used_at` tracking enables detection of unused/compromised keys
- `active` flag allows immediate revocation without deletion
- The `kb_` prefix makes keys identifiable in logs/config (avoids accidental exposure
  of other credentials)

## Future: Migration to Granular Scopes

The current design uses roles (`analyst`, `developer`, `admin`) as a single scope
value. This is intentionally simple for Phase 1. The architecture supports a clean
migration to granular scopes:

### Phase 2 Scope Model (planned, not implemented now)

When an external IdP is introduced or finer-grained control is needed:

1. **Replace role column with scopes array** in `api_keys`:
   ```sql
   ALTER TABLE api_keys DROP COLUMN agent_role;
   ALTER TABLE api_keys ADD COLUMN scopes TEXT[] NOT NULL DEFAULT '{read:public}';
   ```

2. **Define scope taxonomy:**
   | Scope | Replaces | Description |
   |-------|----------|-------------|
   | `read:public` | analyst (partial) | Read public-classified data |
   | `read:internal` | analyst | Read internal + public data |
   | `read:confidential` | admin (partial) | Read confidential + below |
   | `read:restricted` | admin | Read all classification levels |
   | `write:data` | developer | Ingest data, create snapshots |
   | `graph:read` | analyst | Query knowledge graph |
   | `graph:mutate` | developer/admin | Modify knowledge graph |
   | `vault:access` | admin | Access PII vault originals |
   | `admin:keys` | admin | Manage API keys |

3. **Update OPA policies** to check scopes instead of roles:
   ```rego
   # Before (role-based):
   allow if { input.agent_role == "admin" }

   # After (scope-based):
   allow if { "read:confidential" in input.scopes }
   ```

4. **TokenVerifier returns scopes array** instead of role-as-scope.

5. **JWT migration**: When an external IdP issues JWTs, the `scopes` claim maps
   directly to the same scope taxonomy. The `TokenVerifier` implementation changes
   from PG lookup to JWT validation, but the downstream flow is identical.

### Why Not Now

- The existing OPA policies work with 3 roles and are tested
- Scope design requires understanding real access patterns (which we don't have yet)
- The `TokenVerifier` abstraction means the migration is non-breaking
- YAGNI: adding scopes before we need them adds complexity without value

The `TokenVerifier` protocol is the correct abstraction boundary. Everything above
it (middleware, context vars) stays the same regardless of whether we use API keys,
JWTs, or OAuth tokens.
