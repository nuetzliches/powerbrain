# Sprint 4: P2-4 + P2-2 + P3-1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix remaining P2 correctness issues and add retry/circuit-breaker resilience to the MCP server.

**Architecture:** Three independent fixes: (1) Mark P2-4 as resolved (already fixed in code), (2) Add OPA policy filtering to the offline evaluator so it can't leak restricted documents, (3) Add `tenacity` retry decorators to critical external HTTP calls in the MCP server (Ollama, OPA, Qdrant) with exponential backoff.

**Tech Stack:** Python 3.12, tenacity, asyncio, httpx, asyncpg, structural tests (unittest)

---

## Task 1: P2-4 — Mark `business_rules` reference as RESOLVED

The code in `ingestion/snapshot_service.py:36` already omits `business_rules` from `PG_SNAPSHOT_TABLES`. Only the docs need updating.

**Files:**
- Modify: `docs/bekannte-schwachstellen.md:106-118`

**Step 1: Verify the code is already fixed**

```bash
grep -n "business_rules" ingestion/snapshot_service.py
```
Expected: No output (no matches).

**Step 2: Update `bekannte-schwachstellen.md`**

Replace the P2-4 section (lines 106-118) with:

```markdown
### ~~P2-4: Referenz auf nicht existierende Tabelle `business_rules`~~ — RESOLVED

**Status:** RESOLVED — `business_rules` wurde aus `PG_SNAPSHOT_TABLES` in
`snapshot_service.py` entfernt. Business Rules werden ausschließlich über
OPA-Policies (`kb.rules`) bereitgestellt, nicht über PostgreSQL.
```

Also update the Backlog row in the priority table (line 185):

```markdown
| Backlog | P2-2, P2-6, P3-1, P3-2, P3-3 | iterativ |
```

(Remove P2-4 from the Backlog list.)

**Step 3: Commit**

```bash
git add docs/bekannte-schwachstellen.md
git commit -m "docs: mark P2-4 (business_rules reference) as resolved"
```

---

## Task 2: P2-2 — Add OPA policy filtering to `run_eval.py`

**Problem:** `evaluation/run_eval.py` queries Qdrant directly without OPA policy checks.
This means `restricted` or `confidential` documents can leak into `eval_runs.details`.

**Approach:** Add an inline OPA classification filter after the Qdrant search, before
reranking. This keeps the evaluator independent of the MCP server (it's an offline tool)
while enforcing the same access controls. The evaluator uses `EVAL_AGENT_ROLE = "analyst"`
(line 40), so OPA will apply the analyst access rules.

**Files:**
- Modify: `evaluation/run_eval.py:33,40,116-153`
- Create: `tests/test_eval_opa_filter.py`

**Step 1: Write the failing test**

Create `tests/test_eval_opa_filter.py`:

```python
"""Verify run_eval.py applies OPA policy filtering after Qdrant search."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "evaluation" / "run_eval.py"


class TestEvalOPAFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = EVAL_FILE.read_text(encoding="utf-8")

    def test_opa_url_configured(self):
        """run_eval.py must have an OPA_URL configuration."""
        self.assertIn("OPA_URL", self.source,
                       "Must define OPA_URL for policy checks")

    def test_search_filters_by_classification(self):
        """search() must filter Qdrant results by OPA policy."""
        search_start = self.source.index("async def search(")
        # Find the next function definition after search
        next_func = self.source.index("\nasync def ", search_start + 1)
        search_body = self.source[search_start:next_func]
        self.assertIn("classification", search_body,
                       "search() must check classification of results")

    def test_search_calls_opa(self):
        """search() must call OPA to check access policy."""
        search_start = self.source.index("async def search(")
        next_func = self.source.index("\nasync def ", search_start + 1)
        search_body = self.source[search_start:next_func]
        self.assertIn("kb/access", search_body,
                       "search() must call OPA access policy endpoint")

    def test_restricted_docs_filtered(self):
        """search() must remove documents that OPA denies."""
        search_start = self.source.index("async def search(")
        next_func = self.source.index("\nasync def ", search_start + 1)
        search_body = self.source[search_start:next_func]
        # Must have filtering logic (list comprehension or filter)
        has_filter = ("if" in search_body and "allowed" in search_body) or \
                     ("filter" in search_body)
        self.assertTrue(has_filter,
                        "search() must filter out documents where OPA denies access")

    def test_eval_agent_role_used_for_policy(self):
        """OPA check must use EVAL_AGENT_ROLE for consistent evaluation."""
        self.assertIn("EVAL_AGENT_ROLE", self.source,
                       "Must use EVAL_AGENT_ROLE for OPA policy checks")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

```bash
python3.12 -m unittest tests.test_eval_opa_filter -v
```
Expected: FAIL (no OPA_URL, no classification check in search).

**Step 3: Implement the fix**

In `evaluation/run_eval.py`:

1. Add `OPA_URL` config (after line 36):

```python
OPA_URL      = os.getenv("OPA_URL",      "http://opa:8181")
```

2. Add OPA check helper (after `embed_text`, before `search`):

```python
async def check_opa_access(client: httpx.AsyncClient,
                           classification: str) -> bool:
    """Check OPA policy for eval agent access to a classification level."""
    try:
        resp = await client.post(
            f"{OPA_URL}/v1/data/kb/access/allow",
            json={"input": {
                "agent_id": EVAL_AGENT_ID,
                "agent_role": EVAL_AGENT_ROLE,
                "resource": "eval/search",
                "classification": classification,
                "action": "read",
            }},
        )
        resp.raise_for_status()
        return resp.json().get("result", False)
    except Exception as e:
        log.warning(f"OPA check failed, denying access: {e}")
        return False
```

3. In `search()`, after building the `documents` list (after line 137), add OPA filtering:

```python
    # OPA policy filter — remove documents the eval agent may not access
    filtered = []
    for doc in documents:
        classification = doc["metadata"].get("classification", "internal")
        allowed = await check_opa_access(client, classification)
        if allowed:
            filtered.append(doc)
        else:
            log.debug(f"OPA denied eval access to {doc['id']} ({classification})")
    documents = filtered
```

**Step 4: Run test to verify it passes**

```bash
python3.12 -m unittest tests.test_eval_opa_filter -v
```
Expected: PASS (all 5 tests).

**Step 5: Commit**

```bash
git add evaluation/run_eval.py tests/test_eval_opa_filter.py
git commit -m "fix(P2-2): add OPA policy filtering to offline evaluator

run_eval.py now checks each Qdrant result against OPA access policy
before reranking and storing. Prevents restricted/confidential documents
from leaking into eval_runs.details."
```

---

## Task 3: P3-1 — Retry + Circuit Breaker for MCP server

**Problem:** Transient failures in Ollama (~30s model loading), OPA, or Qdrant cause
immediate request failures. No retry logic exists.

**Approach:** Add `tenacity` with exponential backoff to critical functions. Group
services by resilience strategy:

| Service | Strategy | Retries | Wait | Rationale |
|---------|----------|---------|------|-----------|
| Ollama (`embed_text`) | Retry | 3 | 2s, 4s, 8s | Model loading can take 30s |
| OPA (`check_opa_policy`) | Retry | 2 | 0.5s, 1s | Fast service, brief blips |
| Reranker | Fallback (existing) | 0 | — | Already has graceful fallback |
| Qdrant | Retry (client-level) | 2 | 1s, 2s | Via qdrant-client config |
| Ingestion `/scan` | Best-effort | 1 retry | 1s | Non-critical for search |

**Files:**
- Modify: `mcp-server/requirements.txt` (add tenacity)
- Modify: `mcp-server/server.py:182-188` (`embed_text`), `220-239` (`check_opa_policy`), `266-293` (`log_access`)
- Modify: `mcp-server/Dockerfile` (rebuild will pick up new requirements)
- Create: `tests/test_retry_config.py`

**Step 1: Write the failing test**

Create `tests/test_retry_config.py`:

```python
"""Verify retry/circuit-breaker configuration in MCP server."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_FILE = ROOT / "mcp-server" / "server.py"
REQUIREMENTS = ROOT / "mcp-server" / "requirements.txt"


class TestRetryConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_FILE.read_text(encoding="utf-8")
        cls.reqs = REQUIREMENTS.read_text(encoding="utf-8")

    def test_tenacity_in_requirements(self):
        """tenacity must be listed as a dependency."""
        self.assertIn("tenacity", self.reqs,
                       "tenacity must be in mcp-server/requirements.txt")

    def test_tenacity_imported(self):
        """tenacity retry decorators must be imported."""
        self.assertIn("from tenacity import", self.source,
                       "Must import retry utilities from tenacity")

    def test_embed_text_has_retry(self):
        """embed_text must have retry logic for Ollama failures."""
        func_start = self.source.index("async def embed_text")
        # Look for retry decorator in the 200 chars before the function
        pre_func = self.source[max(0, func_start - 200):func_start]
        self.assertIn("retry", pre_func,
                       "embed_text must have a @retry decorator")

    def test_check_opa_policy_has_retry(self):
        """check_opa_policy must have retry logic for OPA failures."""
        func_start = self.source.index("async def check_opa_policy")
        pre_func = self.source[max(0, func_start - 200):func_start]
        self.assertIn("retry", pre_func,
                       "check_opa_policy must have a @retry decorator")

    def test_embed_text_retry_has_backoff(self):
        """embed_text retry must use exponential backoff."""
        func_start = self.source.index("async def embed_text")
        pre_func = self.source[max(0, func_start - 300):func_start]
        self.assertIn("wait_exponential", pre_func,
                       "embed_text retry must use wait_exponential")

    def test_embed_text_retry_has_stop(self):
        """embed_text retry must have a stop condition."""
        func_start = self.source.index("async def embed_text")
        pre_func = self.source[max(0, func_start - 300):func_start]
        self.assertIn("stop_after_attempt", pre_func,
                       "embed_text retry must use stop_after_attempt")

    def test_log_access_scan_is_resilient(self):
        """log_access PII scan must not crash the request on failure."""
        func_start = self.source.index("async def log_access")
        func_end = self.source.index("\n\nasync def ", func_start + 1) if \
            "\n\nasync def " in self.source[func_start + 1:] else \
            self.source.index("\n\n# ", func_start + 1)
        func_body = self.source[func_start:func_end]
        self.assertIn("except", func_body,
                       "log_access must handle /scan failures gracefully")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

```bash
python3.12 -m unittest tests.test_retry_config -v
```
Expected: FAIL (no tenacity, no retry decorators).

**Step 3: Add tenacity to requirements**

In `mcp-server/requirements.txt`, add:

```
tenacity>=9.0
```

**Step 4: Add imports and retry decorators to server.py**

Add import (after existing imports, around line 38):

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
```

Decorate `embed_text` (before line 182):

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=16),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
    before_sleep=lambda rs: log.warning(
        f"Ollama embed retry #{rs.attempt_number} nach Fehler: {rs.outcome.exception()}"
    ),
)
async def embed_text(text: str) -> list[float]:
    ...  # existing body unchanged
```

Decorate `check_opa_policy` (before line 220):

```python
@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
    before_sleep=lambda rs: log.warning(
        f"OPA retry #{rs.attempt_number} nach Fehler: {rs.outcome.exception()}"
    ),
)
async def check_opa_policy(agent_id: str, agent_role: str,
                           resource: str, classification: str,
                           action: str = "read") -> dict:
    ...  # existing body unchanged
```

Wrap the `/scan` call in `log_access` with try/except (around line 272-283):

```python
    if context and "query" in context:
        try:
            scan_resp = await http.post(f"{INGESTION_URL}/scan", json={
                "text": context["query"],
            })
            scan_resp.raise_for_status()
            scan_data = scan_resp.json()

            contains_pii = scan_data["contains_pii"]
            context["query"] = scan_data["masked_text"]
            if contains_pii:
                context["query_contains_pii"] = True
                context["pii_entity_types"] = scan_data["entity_types"]
        except Exception as e:
            log.warning(f"PII scan für Audit-Log fehlgeschlagen, speichere ohne Scan: {e}")
            # Continue without PII scan — better to log unscanned than to fail
```

**Step 5: Run test to verify it passes**

```bash
python3.12 -m unittest tests.test_retry_config -v
```
Expected: PASS (all 7 tests).

**Step 6: Run full test suite**

```bash
python3.12 -m unittest discover tests -v
```
Expected: All existing tests still pass.

**Step 7: Commit**

```bash
git add mcp-server/requirements.txt mcp-server/server.py tests/test_retry_config.py
git commit -m "feat(P3-1): add retry with exponential backoff for Ollama and OPA

- embed_text: 3 attempts, 2/4/8s backoff (Ollama model loading)
- check_opa_policy: 2 attempts, 0.5/1s backoff (brief OPA blips)
- log_access /scan: graceful fallback on PII scanner failure
- tenacity added to requirements"
```

---

## Task 4: Docker rebuild + live verification

**Step 1: Rebuild MCP server**

```bash
docker compose build mcp-server
docker compose up -d mcp-server
```

**Step 2: Verify MCP server starts**

```bash
docker compose logs mcp-server --tail 15
```
Expected: "PostgreSQL pool ready", "Application startup complete"

**Step 3: Test search works E2E**

```bash
curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer kb_dev_localonly_do_not_use_in_production" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"Onboarding","top_k":3}}}' | python3.12 -m json.tool
```
Expected: 3 results, no errors.

**Step 4: Update docs**

Mark P2-2 and P3-1 as resolved in `bekannte-schwachstellen.md`.
Update priority table.

**Step 5: Final commit**

```bash
git add docs/bekannte-schwachstellen.md
git commit -m "docs: mark P2-2, P3-1 as resolved, update sprint status"
```

---

## Summary

| Task | Issue | Aufwand | Abhängigkeiten |
|------|-------|---------|----------------|
| 1 | P2-4 docs update | 2 min | keine |
| 2 | P2-2 OPA filter in eval | 15 min | keine |
| 3 | P3-1 retry/backoff | 20 min | keine |
| 4 | Docker rebuild + verify | 5 min | Task 2, 3 |

All three implementation tasks are independent and can be executed in parallel
(Tasks 1-3), then Task 4 verifies everything together.
