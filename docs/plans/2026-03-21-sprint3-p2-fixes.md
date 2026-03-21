# Sprint 3: P2-1 + P2-5 Fixes

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix two P2 issues — parallelize serial OPA calls (P2-1) and add PG pool lifespan management (P2-5) — to make the system ready for internal testing.

**Architecture:** P2-1 replaces serial `for` loops with `asyncio.gather` for concurrent OPA policy checks across all three affected handlers. P2-5 replaces the lazy `get_pg_pool()` singleton with a proper `lifespan` context manager that initializes the pool at startup with a healthcheck.

**Tech Stack:** Python asyncio, asyncpg, Starlette lifespan, existing unittest structural test pattern.

---

### Task 1: Write test for parallel OPA policy checks (P2-1)

**Files:**
- Create: `tests/test_parallel_opa_checks.py`

**Step 1: Write the test**

```python
"""Verify that OPA policy checks in search handlers use asyncio.gather
instead of serial awaits — P2-1 fix."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_FILE = ROOT / "mcp-server" / "server.py"


class TestParallelOPAChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_FILE.read_text(encoding="utf-8")

    # ── search_knowledge ─────────────────────────────────────
    def test_search_knowledge_uses_gather(self):
        """search_knowledge must use asyncio.gather for OPA checks."""
        # Find the search_knowledge handler section
        sk_start = self.source.index('name == "search_knowledge"')
        # Find the next handler boundary (get_code_context or list_datasets)
        sk_end = self.source.index('name == "get_code_context"', sk_start)
        section = self.source[sk_start:sk_end]

        self.assertIn("asyncio.gather", section,
                       "search_knowledge must use asyncio.gather for OPA checks")
        # Must NOT have serial check_opa_policy in a for loop
        self.assertNotIn("for hit in results.points", section,
                          "search_knowledge must not loop serially over results for OPA checks")

    # ── get_code_context ─────────────────────────────────────
    def test_get_code_context_uses_gather(self):
        """get_code_context must use asyncio.gather for OPA checks."""
        cc_start = self.source.index('name == "get_code_context"')
        cc_end = self.source.index('name == "get_classification"', cc_start)
        section = self.source[cc_start:cc_end]

        self.assertIn("asyncio.gather", section,
                       "get_code_context must use asyncio.gather for OPA checks")

    # ── list_datasets ────────────────────────────────────────
    def test_list_datasets_uses_gather(self):
        """list_datasets must use asyncio.gather for OPA checks."""
        ld_start = self.source.index('name == "list_datasets"')
        ld_end = self.source.index('name == "get_code_context"', ld_start)
        section = self.source[ld_start:ld_end]

        self.assertIn("asyncio.gather", section,
                       "list_datasets must use asyncio.gather for OPA checks")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_parallel_opa_checks.py -v`
Expected: FAIL — `asyncio.gather` not found in source.

---

### Task 2: Write test for PG pool lifespan (P2-5)

**Files:**
- Create: `tests/test_pg_pool_lifespan.py`

**Step 1: Write the test**

```python
"""Verify PG connection pool is initialized in a lifespan context manager
with a startup healthcheck — P2-5 fix."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_FILE = ROOT / "mcp-server" / "server.py"


class TestPGPoolLifespan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_FILE.read_text(encoding="utf-8")

    def test_lifespan_creates_pg_pool(self):
        """PG pool must be created in a lifespan context, not lazily."""
        # There must be an async context manager or lifespan function
        # that calls asyncpg.create_pool
        self.assertIn("asyncpg.create_pool", self.source)
        # The pool creation must happen in a lifespan/startup context
        # (not just in get_pg_pool lazy init)
        # Look for a lifespan function that contains create_pool
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("create_pool", lifespan_section,
                       "asyncpg.create_pool must be called inside the lifespan function")

    def test_startup_healthcheck(self):
        """Lifespan must run SELECT 1 as a startup healthcheck."""
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("SELECT 1", lifespan_section,
                       "Lifespan must run 'SELECT 1' as startup healthcheck")

    def test_pool_closed_on_shutdown(self):
        """PG pool must be closed in lifespan shutdown."""
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("pg_pool.close()", lifespan_section,
                       "Lifespan must close the PG pool on shutdown")

    def test_get_pg_pool_not_lazy(self):
        """get_pg_pool must not lazily create the pool anymore."""
        func_start = self.source.index("def get_pg_pool")
        func_end = self.source.index("\n\n", func_start)
        func_body = self.source[func_start:func_end]
        self.assertNotIn("create_pool", func_body,
                          "get_pg_pool must not lazily create the pool")

    def test_http_client_closed_on_shutdown(self):
        """HTTP client should also be closed in lifespan shutdown."""
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("http.aclose()", lifespan_section,
                       "Lifespan should close the httpx client on shutdown")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pg_pool_lifespan.py -v`
Expected: FAIL — no `async def lifespan` found.

---

### Task 3: Implement P2-5 — PG pool lifespan context manager

**Files:**
- Modify: `mcp-server/server.py`

**Step 1: Add `import asyncio` at top (if missing)**

Ensure `import asyncio` is present in the imports block.

**Step 2: Add `lifespan` async context manager**

Replace the lazy `get_pg_pool` and add a proper lifespan function. Insert after the `get_pg_pool` function (around line 141):

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Startup/shutdown lifecycle: PG pool, HTTP client, MCP session manager."""
    global pg_pool
    # ── Startup ──
    pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
    await pg_pool.fetchval("SELECT 1")
    log.info("PostgreSQL pool ready (%s)", POSTGRES_URL.split("@")[-1])

    async with session_manager.run():
        yield

    # ── Shutdown ──
    await http.aclose()
    await pg_pool.close()
    log.info("Shutdown: PG pool and HTTP client closed")
```

**Step 3: Simplify `get_pg_pool`**

Replace the existing `get_pg_pool` function:

```python
async def get_pg_pool() -> asyncpg.Pool:
    if pg_pool is None:
        raise RuntimeError("PG pool not initialized — server not started via lifespan")
    return pg_pool
```

**Step 4: Wire lifespan into Starlette app**

In the `__main__` block, change:
```python
lifespan=lambda app: session_manager.run(),
```
to:
```python
lifespan=lifespan,
```

Note: `session_manager` must be created before `lifespan` is used, which it already is (line 1229). The `lifespan` function references `session_manager` so it must be defined after `session_manager`. Move the lifespan function definition into `__main__` or make `session_manager` a module-level variable. Simplest: define `lifespan` as a nested function inside `__main__` after `session_manager` is created.

**Step 5: Run test**

Run: `python -m pytest tests/test_pg_pool_lifespan.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add mcp-server/server.py tests/test_pg_pool_lifespan.py
git commit -m "fix(P2-5): initialize PG pool in lifespan with startup healthcheck"
```

---

### Task 4: Implement P2-1 — parallel OPA policy checks

**Files:**
- Modify: `mcp-server/server.py`

**Step 1: Add `import asyncio` (if not already done in Task 3)**

**Step 2: Add helper function `filter_by_policy`**

Insert after `check_opa_policy` (around line 238):

```python
async def filter_by_policy(
    hits: list,
    agent_id: str,
    agent_role: str,
    resource_prefix: str,
) -> list[dict]:
    """Check OPA policies for all hits in parallel, return allowed ones."""
    if not hits:
        return []

    async def _check(hit):
        classification = hit.payload.get("classification", "internal")
        policy = await check_opa_policy(
            agent_id, agent_role,
            f"{resource_prefix}/{hit.id}", classification,
        )
        if policy["allowed"]:
            return hit
        return None

    results = await asyncio.gather(*[_check(h) for h in hits])
    return [h for h in results if h is not None]
```

**Step 3: Refactor `search_knowledge` handler (lines 729-740)**

Replace the serial loop:

```python
        filtered = []
        for hit in results.points:
            classification = hit.payload.get("classification", "internal")
            policy = await check_opa_policy(agent_id, agent_role,
                                            f"{collection}/{hit.id}", classification)
            if policy["allowed"]:
                filtered.append({
                    "id": str(hit.id), "score": round(hit.score, 4),
                    "content": hit.payload.get("text", hit.payload.get("content", "")),
                    "metadata": {k: v for k, v in hit.payload.items()
                                 if k not in ("content", "text")},
                })
```

With:

```python
        allowed_hits = await filter_by_policy(
            results.points, agent_id, agent_role, collection,
        )
        filtered = [
            {
                "id": str(hit.id), "score": round(hit.score, 4),
                "content": hit.payload.get("text", hit.payload.get("content", "")),
                "metadata": {k: v for k, v in hit.payload.items()
                             if k not in ("content", "text")},
            }
            for hit in allowed_hits
        ]
```

**Step 4: Refactor `get_code_context` handler (lines 947-958)**

Replace:

```python
        code_results = []
        for hit in results.points:
            classification = hit.payload.get("classification", "internal")
            policy = await check_opa_policy(agent_id, agent_role, f"code/{hit.id}", classification)
            if policy["allowed"]:
                code_results.append({
                    "id": str(hit.id), "score": round(hit.score, 4),
                    "content": hit.payload.get("content", ""),
                    "metadata": {"repo": hit.payload.get("repo"),
                                 "path": hit.payload.get("path"),
                                 "language": hit.payload.get("language")},
                })
```

With:

```python
        allowed_hits = await filter_by_policy(
            results.points, agent_id, agent_role, "code",
        )
        code_results = [
            {
                "id": str(hit.id), "score": round(hit.score, 4),
                "content": hit.payload.get("content", ""),
                "metadata": {"repo": hit.payload.get("repo"),
                             "path": hit.payload.get("path"),
                             "language": hit.payload.get("language")},
            }
            for hit in allowed_hits
        ]
```

**Step 5: Refactor `list_datasets` handler (lines 913-922)**

This one is different — it iterates DB rows, not Qdrant hits. Add a separate parallel helper or inline `asyncio.gather`. The rows don't have `.payload` so we need a slightly different approach:

Replace:

```python
        datasets = []
        for r in rows:
            policy = await check_opa_policy(agent_id, agent_role,
                                            f"dataset/{r['id']}", r["classification"])
            if policy["allowed"]:
                datasets.append({
                    "id": str(r["id"]), "name": r["name"],
                    "project": r["project"], "classification": r["classification"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                })
```

With:

```python
        async def _check_dataset(r):
            policy = await check_opa_policy(
                agent_id, agent_role,
                f"dataset/{r['id']}", r["classification"],
            )
            if policy["allowed"]:
                return {
                    "id": str(r["id"]), "name": r["name"],
                    "project": r["project"], "classification": r["classification"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
            return None

        checked = await asyncio.gather(*[_check_dataset(r) for r in rows])
        datasets = [d for d in checked if d is not None]
```

**Step 6: Run tests**

Run: `python -m pytest tests/test_parallel_opa_checks.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add mcp-server/server.py tests/test_parallel_opa_checks.py
git commit -m "fix(P2-1): parallelize OPA policy checks with asyncio.gather"
```

---

### Task 5: Update `bekannte-schwachstellen.md`

**Files:**
- Modify: `docs/bekannte-schwachstellen.md`

**Step 1: Mark P0-1 through P0-4 as RESOLVED**

Add `~~` strikethrough and RESOLVED status to each P0 entry (same pattern as P1 entries).

**Step 2: Mark P2-1 and P2-5 as RESOLVED**

Add RESOLVED status with brief description of the fix.

**Step 3: Mark P2-3 as RESOLVED**

Already fixed (ingestion_api.py exists with `/snapshots/create`).

**Step 4: Update Priorisierung table**

Sprint 1 and Sprint 3 as resolved.

**Step 5: Commit**

```bash
git add docs/bekannte-schwachstellen.md
git commit -m "docs: mark P0-1..P0-4, P2-1, P2-3, P2-5 as resolved"
```

---

### Task 6: Run full test suite

**Step 1: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS.
