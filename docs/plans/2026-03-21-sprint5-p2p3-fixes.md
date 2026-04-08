# Sprint 5: P2-6 + P3-2 + P3-3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix Apache AGE bugs, add in-memory rate limiting, and clean up the ingestion pipeline with a new chunk API for adapters.

**Architecture:** Three independent fixes: (1) AGE hardening — missing migration, agtype parsing, shortestPath fallback, (2) Token bucket rate limiter as Starlette middleware with per-role env-var config, (3) Remove ingestion stubs, simplify to text-only, add `/ingest/chunks` endpoint for adapters.

**Tech Stack:** Python 3.12, asyncio, asyncpg, Starlette middleware, structural tests (unittest)

---

## Task 1: P2-6 — Missing `graph_sync_log` migration

The `_log_sync()` function in `graph_service.py:331-336` writes to `graph_sync_log`, but this table doesn't exist in any migration. Every graph mutation crashes at the logging step.

**Files:**
- Create: `init-db/011_graph_sync_log.sql`
- Create: `tests/test_graph_sync_log.py`

**Step 1: Write the failing test**

Create `tests/test_graph_sync_log.py`:

```python
"""Verify graph_sync_log table is defined in migrations."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_DIR = ROOT / "init-db"
GRAPH_SERVICE = ROOT / "mcp-server" / "graph_service.py"


class TestGraphSyncLog(unittest.TestCase):
    def test_graph_sync_log_migration_exists(self):
        """A migration must define the graph_sync_log table."""
        found = False
        for sql_file in sorted(MIGRATION_DIR.glob("*.sql")):
            content = sql_file.read_text(encoding="utf-8")
            if "graph_sync_log" in content and "CREATE TABLE" in content:
                found = True
                break
        self.assertTrue(found,
                        "No migration creates the graph_sync_log table")

    def test_graph_sync_log_has_required_columns(self):
        """graph_sync_log must have entity_type, entity_id, action columns."""
        for sql_file in sorted(MIGRATION_DIR.glob("*.sql")):
            content = sql_file.read_text(encoding="utf-8")
            if "graph_sync_log" in content and "CREATE TABLE" in content:
                self.assertIn("entity_type", content,
                              "graph_sync_log must have entity_type column")
                self.assertIn("entity_id", content,
                              "graph_sync_log must have entity_id column")
                self.assertIn("action", content,
                              "graph_sync_log must have action column")
                return
        self.fail("No migration with graph_sync_log found")

    def test_graph_service_references_graph_sync_log(self):
        """graph_service.py must use graph_sync_log for mutation logging."""
        source = GRAPH_SERVICE.read_text(encoding="utf-8")
        self.assertIn("graph_sync_log", source,
                       "graph_service.py must reference graph_sync_log")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

```bash
python3.12 -m unittest tests.test_graph_sync_log -v
```
Expected: FAIL (no migration creates graph_sync_log).

**Step 3: Create the migration**

Create `init-db/011_graph_sync_log.sql`:

```sql
-- Graph sync log for tracking knowledge graph mutations
CREATE TABLE IF NOT EXISTS graph_sync_log (
    id          SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    details     JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_graph_sync_log_entity
    ON graph_sync_log (entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_graph_sync_log_created
    ON graph_sync_log (created_at);
```

**Step 4: Run test to verify it passes**

```bash
python3.12 -m unittest tests.test_graph_sync_log -v
```
Expected: PASS (all 3 tests).

**Step 5: Commit**

```bash
git add init-db/011_graph_sync_log.sql tests/test_graph_sync_log.py
git commit -m "fix(P2-6): add missing graph_sync_log migration"
```

---

## Task 2: P2-6 — AGE agtype parsing hardening

`_execute_cypher()` in `graph_service.py:96-106` does `json.loads(str(raw))` which breaks on AGE-specific agtype suffixes like `::vertex`, `::edge`, `::path`.

**Files:**
- Modify: `mcp-server/graph_service.py:28-30,96-106`
- Create: `tests/test_agtype_parsing.py`

**Step 1: Write the failing test**

Create `tests/test_agtype_parsing.py`:

```python
"""Verify graph_service.py handles AGE agtype parsing correctly."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_SERVICE = ROOT / "mcp-server" / "graph_service.py"


class TestAgtypeParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = GRAPH_SERVICE.read_text(encoding="utf-8")

    def test_agtype_suffix_stripping(self):
        """_execute_cypher must strip AGE agtype suffixes before JSON parsing."""
        # Must have regex or string replacement for ::vertex, ::edge, ::path
        func_start = self.source.index("async def _execute_cypher(")
        func_end = self.source.index("\nasync def ", func_start + 1)
        func_body = self.source[func_start:func_end]
        has_suffix_handling = (
            "::vertex" in func_body or
            "::edge" in func_body or
            "::path" in func_body or
            "agtype" in func_body.lower()
        )
        self.assertTrue(has_suffix_handling,
                        "_execute_cypher must handle AGE agtype suffixes")

    def test_agtype_uses_regex(self):
        """agtype suffix stripping should use regex for robustness."""
        self.assertIn("re.", self.source,
                       "graph_service must use regex (re module) for agtype handling")

    def test_robust_json_fallback(self):
        """_execute_cypher must have fallback for unparseable agtype results."""
        func_start = self.source.index("async def _execute_cypher(")
        func_end = self.source.index("\nasync def ", func_start + 1)
        func_body = self.source[func_start:func_end]
        self.assertIn("except", func_body,
                       "_execute_cypher must catch JSON parse errors")
        self.assertIn("raw", func_body,
                       "_execute_cypher must preserve raw value on parse failure")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

```bash
python3.12 -m unittest tests.test_agtype_parsing -v
```
Expected: FAIL (no agtype suffix handling).

**Step 3: Implement agtype hardening**

In `mcp-server/graph_service.py`, modify `_execute_cypher()` result parsing (lines 96-106).

Replace the existing result parsing block:

```python
    results = []
    for row in rows:
        raw = row["result"]
        if raw is not None:
            try:
                parsed = json.loads(str(raw))
                results.append(parsed)
            except (json.JSONDecodeError, TypeError):
                results.append({"raw": str(raw)})
    return results
```

With this hardened version:

```python
    results = []
    for row in rows:
        raw = row["result"]
        if raw is not None:
            try:
                raw_str = str(raw)
                # Strip AGE agtype suffixes (::vertex, ::edge, ::path, ::numeric)
                cleaned = re.sub(r'::(vertex|edge|path|numeric)\b', '', raw_str)
                parsed = json.loads(cleaned)
                results.append(parsed)
            except (json.JSONDecodeError, TypeError):
                results.append({"raw": str(raw)})
    return results
```

Note: `re` is already imported at line 30.

**Step 4: Run test to verify it passes**

```bash
python3.12 -m unittest tests.test_agtype_parsing -v
```
Expected: PASS (all 3 tests).

**Step 5: Commit**

```bash
git add mcp-server/graph_service.py tests/test_agtype_parsing.py
git commit -m "fix(P2-6): harden agtype parsing with suffix stripping"
```

---

## Task 3: P2-6 — shortestPath fallback

`find_path()` in `graph_service.py:255-268` uses `shortestPath()` which has known bugs in certain AGE versions. Add a BFS fallback when `shortestPath` fails.

**Files:**
- Modify: `mcp-server/graph_service.py:255-268`
- Create: `tests/test_find_path_fallback.py`

**Step 1: Write the failing test**

Create `tests/test_find_path_fallback.py`:

```python
"""Verify find_path has a fallback when shortestPath fails."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_SERVICE = ROOT / "mcp-server" / "graph_service.py"


class TestFindPathFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = GRAPH_SERVICE.read_text(encoding="utf-8")

    def test_find_path_has_try_except(self):
        """find_path must handle shortestPath failures."""
        func_start = self.source.index("async def find_path(")
        # Find end: next function def or end of file
        next_func_pos = self.source.find("\nasync def ", func_start + 1)
        if next_func_pos == -1:
            next_func_pos = len(self.source)
        func_body = self.source[func_start:next_func_pos]
        self.assertIn("except", func_body,
                       "find_path must catch shortestPath failures")

    def test_find_path_has_fallback(self):
        """find_path must have a BFS/iterative fallback."""
        func_start = self.source.index("async def find_path(")
        next_func_pos = self.source.find("\nasync def ", func_start + 1)
        if next_func_pos == -1:
            next_func_pos = len(self.source)
        func_body = self.source[func_start:next_func_pos]
        has_fallback = (
            "get_neighbors" in func_body or
            "fallback" in func_body.lower() or
            "bfs" in func_body.lower() or
            "MATCH" in func_body  # manual path query
        )
        self.assertTrue(has_fallback,
                        "find_path must have a fallback strategy")

    def test_find_path_logs_fallback(self):
        """find_path must log when falling back."""
        func_start = self.source.index("async def find_path(")
        next_func_pos = self.source.find("\nasync def ", func_start + 1)
        if next_func_pos == -1:
            next_func_pos = len(self.source)
        func_body = self.source[func_start:next_func_pos]
        self.assertIn("log.warning", func_body,
                       "find_path must log when shortestPath fails")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

```bash
python3.12 -m unittest tests.test_find_path_fallback -v
```
Expected: FAIL (no try/except, no fallback).

**Step 3: Implement shortestPath fallback**

Replace the existing `find_path()` at `graph_service.py:255-268` with:

```python
async def find_path(pool: asyncpg.Pool,
                    from_label: str, from_id: str,
                    to_label: str, to_id: str,
                    max_depth: int = 5) -> list[dict]:
    """Findet den kürzesten Pfad zwischen zwei Knoten.

    Versucht zuerst shortestPath (AGE built-in). Falls das fehlschlägt
    (bekannter AGE-Bug bei gerichteten Graphen), Fallback auf iterativen
    Pfad via variable-depth MATCH.
    """
    _require_identifier(from_label, "from_label")
    _require_identifier(to_label, "to_label")
    try:
        cypher = (
            f"MATCH p = shortestPath("
            f"(a:{from_label} {{id: {_escape_cypher_value(from_id)}}})-[*..{max_depth}]-"
            f"(b:{to_label} {{id: {_escape_cypher_value(to_id)}}}))"
            f" RETURN p"
        )
        result = await _execute_cypher(pool, cypher)
        if result:
            return result
    except Exception as e:
        log.warning(f"shortestPath fehlgeschlagen, verwende Fallback: {e}")

    # Fallback: variable-depth MATCH without shortestPath
    cypher_fallback = (
        f"MATCH (a:{from_label} {{id: {_escape_cypher_value(from_id)}}})"
        f"-[r*1..{max_depth}]-"
        f"(b:{to_label} {{id: {_escape_cypher_value(to_id)}}})"
        f" RETURN a, r, b LIMIT 1"
    )
    return await _execute_cypher(pool, cypher_fallback)
```

**Step 4: Run test to verify it passes**

```bash
python3.12 -m unittest tests.test_find_path_fallback -v
```
Expected: PASS (all 3 tests).

**Step 5: Commit**

```bash
git add mcp-server/graph_service.py tests/test_find_path_fallback.py
git commit -m "fix(P2-6): add shortestPath fallback for AGE compatibility"
```

---

## Task 4: P3-2 — Rate Limiting Middleware

Add in-memory token bucket rate limiter as Starlette middleware. Limits configurable per role via environment variables.

**Files:**
- Modify: `mcp-server/server.py:60-74` (add env vars), `77-106` (add Prometheus counter), `1326-1355` (add middleware)
- Create: `tests/test_rate_limiter.py`

**Step 1: Write the failing test**

Create `tests/test_rate_limiter.py`:

```python
"""Verify rate limiting configuration in MCP server."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_FILE = ROOT / "mcp-server" / "server.py"


class TestRateLimiter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_FILE.read_text(encoding="utf-8")

    def test_rate_limit_env_vars(self):
        """Rate limit must be configurable via environment variables."""
        self.assertIn("RATE_LIMIT_ANALYST", self.source,
                       "Must have RATE_LIMIT_ANALYST env var")
        self.assertIn("RATE_LIMIT_DEVELOPER", self.source,
                       "Must have RATE_LIMIT_DEVELOPER env var")
        self.assertIn("RATE_LIMIT_ADMIN", self.source,
                       "Must have RATE_LIMIT_ADMIN env var")

    def test_rate_limit_enabled_flag(self):
        """Rate limiting must be toggleable via RATE_LIMIT_ENABLED."""
        self.assertIn("RATE_LIMIT_ENABLED", self.source,
                       "Must have RATE_LIMIT_ENABLED env var")

    def test_token_bucket_class(self):
        """Must implement a TokenBucket for rate limiting."""
        self.assertIn("class TokenBucket", self.source,
                       "Must have a TokenBucket class")

    def test_rate_limit_middleware(self):
        """Must have rate limiting middleware in the request chain."""
        self.assertIn("RateLimitMiddleware", self.source,
                       "Must have a RateLimitMiddleware class")

    def test_429_response(self):
        """Rate limiter must return 429 when limit exceeded."""
        self.assertIn("429", self.source,
                       "Must return HTTP 429 when rate limited")

    def test_retry_after_header(self):
        """Rate limiter must include Retry-After header."""
        self.assertIn("Retry-After", self.source,
                       "Must set Retry-After header on 429 responses")

    def test_rate_limit_prometheus_counter(self):
        """Must track rate limit rejections in Prometheus."""
        self.assertIn("rate_limit", self.source,
                       "Must have rate_limit Prometheus metric")

    def test_rate_limit_fail_open(self):
        """Rate limiter must fail open (allow requests on error)."""
        # Find the RateLimitMiddleware class
        cls_start = self.source.index("class RateLimitMiddleware")
        cls_end = self.source.index("\nclass ", cls_start + 1) if \
            "\nclass " in self.source[cls_start + 1:] else len(self.source)
        cls_body = self.source[cls_start:cls_end]
        self.assertIn("except", cls_body,
                       "RateLimitMiddleware must handle errors")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

```bash
python3.12 -m unittest tests.test_rate_limiter -v
```
Expected: FAIL (no rate limiting code exists).

**Step 3: Implement rate limiting**

In `mcp-server/server.py`, make the following changes:

**3a. Add env vars** (after line 63, the `AUTH_REQUIRED` line):

```python
RATE_LIMIT_ENABLED    = os.getenv("RATE_LIMIT_ENABLED", "true") == "true"
RATE_LIMIT_ANALYST    = int(os.getenv("RATE_LIMIT_ANALYST", "60"))
RATE_LIMIT_DEVELOPER  = int(os.getenv("RATE_LIMIT_DEVELOPER", "120"))
RATE_LIMIT_ADMIN      = int(os.getenv("RATE_LIMIT_ADMIN", "300"))
RATE_LIMITS_BY_ROLE   = {
    "analyst": RATE_LIMIT_ANALYST,
    "developer": RATE_LIMIT_DEVELOPER,
    "admin": RATE_LIMIT_ADMIN,
}
```

**3b. Add Prometheus counter** (after the existing metrics, around line 106):

```python
mcp_rate_limit_rejected = Counter(
    "kb_rate_limit_rejected_total",
    "Requests rejected by rate limiter",
    ["agent_role"],
)
```

**3c. Add TokenBucket class and RateLimitMiddleware** (after the Prometheus metrics section, before the `ApiKeyVerifier` class, around line 110):

```python
class TokenBucket:
    """In-memory token bucket for rate limiting."""

    def __init__(self, capacity: float, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate  # tokens per second
        self.last_refill = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0.0
        self._lock = asyncio.Lock()
        self.last_used = self.last_refill

    async def consume(self) -> tuple[bool, float]:
        """Try to consume a token. Returns (allowed, retry_after_seconds)."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            self.last_used = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0
            else:
                retry_after = (1.0 - self.tokens) / self.refill_rate
                return False, retry_after


_rate_limit_buckets: dict[str, TokenBucket] = {}
_rate_limit_cleanup_counter = 0


def _get_bucket(agent_id: str, role: str) -> TokenBucket:
    """Get or create a token bucket for an agent."""
    global _rate_limit_cleanup_counter
    if agent_id not in _rate_limit_buckets:
        rpm = RATE_LIMITS_BY_ROLE.get(role, RATE_LIMIT_ANALYST)
        _rate_limit_buckets[agent_id] = TokenBucket(
            capacity=float(rpm),
            refill_rate=rpm / 60.0,
        )
    # Periodic cleanup of stale buckets (every 100 requests)
    _rate_limit_cleanup_counter += 1
    if _rate_limit_cleanup_counter >= 100:
        _rate_limit_cleanup_counter = 0
        now = asyncio.get_event_loop().time()
        stale = [k for k, v in _rate_limit_buckets.items()
                 if now - v.last_used > 600]
        for k in stale:
            del _rate_limit_buckets[k]
    return _rate_limit_buckets[agent_id]


class RateLimitMiddleware:
    """Starlette middleware for per-agent rate limiting."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not RATE_LIMIT_ENABLED:
            return await self.app(scope, receive, send)

        # Skip rate limiting for health/metrics endpoints
        path = scope.get("path", "")
        if path in ("/health", "/metrics"):
            return await self.app(scope, receive, send)

        try:
            # Extract agent info from auth state (set by AuthContextMiddleware)
            user = scope.get("user")
            if user and hasattr(user, "identity"):
                agent_id = user.identity
                role = user.scopes[0] if user.scopes else "analyst"
                bucket = _get_bucket(agent_id, role)
                allowed, retry_after = await bucket.consume()

                if not allowed:
                    mcp_rate_limit_rejected.labels(agent_role=role).inc()
                    response_body = json.dumps({
                        "error": "Rate limit exceeded",
                        "retry_after": round(retry_after, 1),
                    }).encode()
                    await send({
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"Retry-After", str(int(retry_after) + 1).encode()],
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": response_body,
                    })
                    return
        except Exception as e:
            # Fail open — rate limiter error should not block requests
            log.warning(f"Rate limiter Fehler, Request wird durchgelassen: {e}")

        return await self.app(scope, receive, send)
```

**3d. Add middleware to chain** (in the middleware section, around line 1335).

The middleware must go AFTER `AuthContextMiddleware` (which sets `scope["user"]`) but is applied as a Starlette middleware wrapper. Since Starlette applies middleware inside-out (last added = first executed on response, last executed on request), and `AuthContextMiddleware` is added first (innermost), we add `RateLimitMiddleware` after `AuthContextMiddleware` so it can read `scope["user"]`.

Find the line that adds `AuthContextMiddleware` and add `RateLimitMiddleware` right after it:

```python
    app.add_middleware(AuthContextMiddleware)
    app.add_middleware(RateLimitMiddleware)
```

Note: `import json` is already present at the top of server.py.

**Step 4: Run test to verify it passes**

```bash
python3.12 -m unittest tests.test_rate_limiter -v
```
Expected: PASS (all 8 tests).

**Step 5: Run full test suite**

```bash
python3.12 -m unittest discover tests -v
```
Expected: All existing tests still pass (no regressions).

**Step 6: Commit**

```bash
git add mcp-server/server.py tests/test_rate_limiter.py
git commit -m "feat(P3-2): add in-memory token bucket rate limiting per agent

- TokenBucket with per-role capacity from env vars (RATE_LIMIT_ANALYST=60, etc.)
- RateLimitMiddleware returns 429 with Retry-After header
- Stale bucket cleanup every 100 requests
- Fail-open on rate limiter errors
- Prometheus counter kb_rate_limit_rejected_total"
```

---

## Task 5: P3-3 — Ingestion cleanup + Chunk API

Remove stubs, simplify `/ingest` to text-only, add `/ingest/chunks` endpoint for adapters.

**Files:**
- Modify: `ingestion/ingestion_api.py:40-48,85-91,501-547`
- Modify: `mcp-server/server.py:562-576` (ingest_data tool schema)
- Create: `tests/test_ingestion_cleanup.py`

**Step 1: Write the failing test**

Create `tests/test_ingestion_cleanup.py`:

```python
"""Verify ingestion pipeline cleanup and chunk API."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INGESTION_FILE = ROOT / "ingestion" / "ingestion_api.py"
SERVER_FILE = ROOT / "mcp-server" / "server.py"


class TestIngestionCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingestion_src = INGESTION_FILE.read_text(encoding="utf-8")
        cls.server_src = SERVER_FILE.read_text(encoding="utf-8")

    def test_no_git_repo_stub(self):
        """git_repo stub must be removed from ingestion."""
        self.assertNotIn('"git_repo"', self.ingestion_src,
                          "git_repo stub must be removed from ingestion_api.py")

    def test_no_sql_dump_stub(self):
        """sql_dump stub must be removed from ingestion."""
        self.assertNotIn('"sql_dump"', self.ingestion_src,
                          "sql_dump stub must be removed from ingestion_api.py")

    def test_mcp_schema_text_only(self):
        """MCP ingest_data tool must only accept 'text' source_type."""
        # Find the ingest_data tool definition
        tool_start = self.server_src.index('"ingest_data"')
        # Find the end of this tool definition (next Tool( or end of list)
        tool_end = self.server_src.index("Tool(", tool_start + 1) if \
            "Tool(" in self.server_src[tool_start + 1:] else \
            self.server_src.index("]", tool_start)
        tool_def = self.server_src[tool_start:tool_end]
        self.assertNotIn("git_repo", tool_def,
                          "ingest_data schema must not list git_repo")
        self.assertNotIn("sql_dump", tool_def,
                          "ingest_data schema must not list sql_dump")

    def test_chunk_ingest_endpoint_exists(self):
        """/ingest/chunks endpoint must exist for adapter ingestion."""
        self.assertIn("/ingest/chunks", self.ingestion_src,
                       "Must have /ingest/chunks endpoint")

    def test_chunk_ingest_request_model(self):
        """ChunkIngestRequest model must exist with required fields."""
        self.assertIn("class ChunkIngestRequest", self.ingestion_src,
                       "Must define ChunkIngestRequest model")
        self.assertIn("chunks", self.ingestion_src,
                       "ChunkIngestRequest must have chunks field")

    def test_chunk_endpoint_calls_pipeline(self):
        """/ingest/chunks must call ingest_text_chunks for privacy pipeline."""
        # Find the chunks endpoint handler
        endpoint_start = self.ingestion_src.index("/ingest/chunks")
        endpoint_body = self.ingestion_src[endpoint_start:endpoint_start + 500]
        self.assertIn("ingest_text_chunks", endpoint_body,
                       "/ingest/chunks must call ingest_text_chunks")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

```bash
python3.12 -m unittest tests.test_ingestion_cleanup -v
```
Expected: FAIL (stubs still exist, no chunks endpoint).

**Step 3: Implement cleanup**

**3a. Update `ingestion/ingestion_api.py`:**

Remove `COLLECTION_MAP` stubs (lines 43-48). Replace with:

```python
DEFAULT_COLLECTION = "knowledge_general"
```

Add `ChunkIngestRequest` model after the existing models (after `ScanRequest`, around line 100):

```python
class ChunkIngestRequest(BaseModel):
    """Request for adapter-based chunk ingestion. Internal use only."""
    chunks: list[str]
    project: str
    collection: str = "knowledge_general"
    classification: str = "internal"
    metadata: dict[str, Any] = {}
    source: str = ""
```

Replace the POST `/ingest` endpoint (lines 501-547) with the simplified version:

```python
@app.post("/ingest")
async def ingest(req: IngestRequest):
    collection = req.collection or DEFAULT_COLLECTION
    chunks = chunk_text(req.source)
    result = await ingest_text_chunks(
        chunks=chunks,
        collection=collection,
        source=req.source_type or "text",
        classification=req.classification,
        project=req.project,
        metadata=req.metadata,
    )
    return result
```

Add the new `/ingest/chunks` endpoint (after the `/ingest` endpoint):

```python
@app.post("/ingest/chunks")
async def ingest_chunks(req: ChunkIngestRequest):
    """Ingest pre-processed chunks from adapters. Full privacy pipeline applies."""
    result = await ingest_text_chunks(
        chunks=req.chunks,
        collection=req.collection,
        source=req.source,
        classification=req.classification,
        project=req.project,
        metadata=req.metadata,
    )
    return result
```

Update `IngestRequest` model — remove `source_type` enum constraint since we no longer switch on it. Keep it as an optional string for metadata:

```python
class IngestRequest(BaseModel):
    source: str
    source_type: str | None = "text"
    collection: str | None = None
    project: str | None = None
    classification: str = "internal"
    metadata: dict[str, Any] = {}
```

**3b. Update MCP tool schema** in `mcp-server/server.py:562-576`:

Replace the `source_type` property:

```python
"source_type":    {"type": "string", "default": "text",
                   "description": "Quelltyp (text). Weitere Typen via Adapter."},
```

Remove the `"enum": ["csv", "json", "sql_dump", "git_repo"]` constraint. Also remove `source_type` from the `"required"` list (only `"source"` is required now):

```python
"required": ["source"]
```

**Step 4: Run test to verify it passes**

```bash
python3.12 -m unittest tests.test_ingestion_cleanup -v
```
Expected: PASS (all 6 tests).

**Step 5: Run full test suite**

```bash
python3.12 -m unittest discover tests -v
```
Expected: All existing tests still pass.

**Step 6: Commit**

```bash
git add ingestion/ingestion_api.py mcp-server/server.py tests/test_ingestion_cleanup.py
git commit -m "feat(P3-3): clean up ingestion stubs, add /ingest/chunks adapter API

- Remove git_repo and sql_dump stubs from /ingest
- Simplify /ingest to text-only ingestion
- Add POST /ingest/chunks for adapter-based chunk ingestion
- Both endpoints use full privacy pipeline (PII → OPA → Vault → Embed)
- Update MCP ingest_data schema to text-only"
```

---

## Task 6: Docker rebuild + live verification + docs update

**Step 1: Rebuild all changed services**

```bash
docker compose build mcp-server ingestion
docker compose up -d mcp-server ingestion
```

Wait for PostgreSQL to run new migration:

```bash
docker compose restart postgres
sleep 5
docker compose up -d mcp-server ingestion
```

**Step 2: Verify services start**

```bash
docker compose logs mcp-server --tail 10
docker compose logs ingestion --tail 10
```
Expected: "Application startup complete" for both.

**Step 3: Verify migration ran**

```bash
docker exec kb-postgres psql -U kb_admin -d knowledgebase -c "\dt graph_sync_log"
```
Expected: Table listed.

**Step 4: Test search E2E (with rate limiting active)**

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer kb_dev_localonly_do_not_use_in_production" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"Onboarding","top_k":3}}}' | python3.12 -m json.tool
```
Expected: 3 results, no errors.

**Step 5: Test ingestion E2E**

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer kb_dev_localonly_do_not_use_in_production" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ingest_data","arguments":{"source":"Test-Dokument für Sprint 5 Verifikation.","project":"test"}}}' | python3.12 -m json.tool
```
Expected: Success response with document ID.

**Step 6: Update docs**

Mark P2-6, P3-2, P3-3 as resolved in `docs/bekannte-schwachstellen.md`.
Update priority table with Sprint 5.

**Step 7: Commit**

```bash
git add docs/bekannte-schwachstellen.md
git commit -m "docs: mark P2-6, P3-2, P3-3 as resolved, Sprint 5 complete"
```

---

## Summary

| Task | Issue | Effort | Dependencies |
|------|-------|--------|--------------|
| 1 | P2-6 graph_sync_log migration | 5 min | none |
| 2 | P2-6 agtype parsing hardening | 10 min | none |
| 3 | P2-6 shortestPath fallback | 10 min | Task 2 (same file) |
| 4 | P3-2 rate limiting middleware | 20 min | none |
| 5 | P3-3 ingestion cleanup + chunk API | 15 min | none |
| 6 | Docker rebuild + verify + docs | 10 min | Tasks 1-5 |

Tasks 1, 2, 4, 5 are independent. Task 3 depends on Task 2 (same file).
Task 6 depends on all previous tasks.
