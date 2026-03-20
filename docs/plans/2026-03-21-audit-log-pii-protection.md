# Audit-Log PII Protection — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Protect audit logs from storing raw PII by scanning query text before logging, securing the audit table with RLS, and adding time-based retention anonymization.

**Architecture:** The ingestion service gets a `/scan` endpoint exposing the existing PIIScanner. The MCP server calls `/scan` before writing audit log entries with query text. No fallback — scanner unavailability is an error. RLS on `agent_access_log` restricts access. A new retention job anonymizes old entries.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, httpx, Microsoft Presidio, PostgreSQL 16 RLS

**Test runner:** `python3 -m unittest discover -s tests -v` (no pytest; external deps like asyncpg/httpx only in Docker)

**Pre-existing test failure:** `test_mcp_server_no_longer_uses_stdio_transport` fails — this is NOT caused by our changes, ignore it.

---

### Task 1: Ingestion `/scan` Endpoint

**Files:**
- Modify: `ingestion/ingestion_api.py` (add endpoint after line ~80)
- Test: `tests/test_audit_pii_protection.py` (create)

**Step 1: Write the failing test**

Create `tests/test_audit_pii_protection.py`:

```python
"""
Tests for Audit-Log PII Protection
===================================
Structural + unit tests for the /scan endpoint, log_access PII scanning,
audit RLS migration, and retention anonymization.

External deps (httpx, asyncpg, presidio) are only in Docker —
tests use structural analysis or exec()-based extraction.
"""

import unittest
import os
import re


class TestScanEndpoint(unittest.TestCase):
    """Structural tests: ingestion_api.py must have a /scan endpoint."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__),
                            "..", "ingestion", "ingestion_api.py")
        with open(path) as f:
            cls.source = f.read()

    def test_scan_endpoint_exists(self):
        """POST /scan endpoint must be defined."""
        self.assertIn('@app.post("/scan")', self.source)

    def test_scan_endpoint_calls_scanner(self):
        """The /scan handler must use get_scanner() or PIIScanner."""
        # Must call scan_text and mask_text
        self.assertIn("scan_text", self.source)
        self.assertIn("mask_text", self.source)

    def test_scan_request_model_has_text_field(self):
        """A ScanRequest Pydantic model must exist with a text field."""
        self.assertIn("class ScanRequest", self.source)
        self.assertIn("text:", self.source)

    def test_scan_response_has_required_fields(self):
        """Response must include contains_pii, masked_text, entity_types."""
        self.assertIn("contains_pii", self.source)
        self.assertIn("masked_text", self.source)
        self.assertIn("entity_types", self.source)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestScanEndpoint -v`
Expected: FAIL — endpoint does not exist yet.

**Step 3: Write minimal implementation**

In `ingestion/ingestion_api.py`, add after the existing Pydantic models (around line 80):

```python
# ── Scan-Only Endpoint ───────────────────────────────────────

class ScanRequest(BaseModel):
    """Request for PII scanning without ingestion."""
    text: str = Field(..., min_length=1, description="Text to scan for PII")
    language: str = Field(default="de", description="Language code (de, en)")


class ScanResponse(BaseModel):
    """PII scan result."""
    contains_pii: bool
    masked_text: str
    entity_types: list[str]


@app.post("/scan", response_model=ScanResponse)
async def scan_text_endpoint(req: ScanRequest) -> ScanResponse:
    """
    Scan text for PII without ingesting it.
    Used by MCP server to sanitize audit log entries.
    """
    scanner = get_scanner()
    result = scanner.scan_text(req.text, language=req.language)

    if result.contains_pii:
        masked = scanner.mask_text(req.text, language=req.language)
        entity_types = list(result.entity_counts.keys())
    else:
        masked = req.text
        entity_types = []

    return ScanResponse(
        contains_pii=result.contains_pii,
        masked_text=masked,
        entity_types=entity_types,
    )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestScanEndpoint -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add ingestion/ingestion_api.py tests/test_audit_pii_protection.py
git commit -m "feat: add /scan endpoint to ingestion service for PII detection"
```

---

### Task 2: MCP Server — Use INGESTION_URL Env Var

**Files:**
- Modify: `mcp-server/server.py` (lines 38-46 config block, lines 836, 1133)
- Test: `tests/test_audit_pii_protection.py` (add test class)

**Step 1: Write the failing test**

Add to `tests/test_audit_pii_protection.py`:

```python
class TestMcpServerIngestionUrl(unittest.TestCase):
    """MCP server must use INGESTION_URL env var, not hardcoded URL."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__),
                            "..", "mcp-server", "server.py")
        with open(path) as f:
            cls.source = f.read()

    def test_ingestion_url_env_var_defined(self):
        """INGESTION_URL must be read from environment."""
        self.assertRegex(self.source, r'INGESTION_URL\s*=\s*os\.getenv\(')

    def test_no_hardcoded_ingestion_url(self):
        """No hardcoded http://ingestion:8081 in tool handlers."""
        # The env var default is ok, but direct string usage in http.post is not
        lines = self.source.split('\n')
        for i, line in enumerate(lines, 1):
            if 'http.post(' in line and '"http://ingestion:8081' in line:
                self.fail(f"Line {i}: hardcoded ingestion URL in http.post call")
```

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestMcpServerIngestionUrl -v`
Expected: FAIL — hardcoded URLs exist on lines 836 and 1133.

**Step 3: Write minimal implementation**

In `mcp-server/server.py`, add to config block (after line 46):

```python
INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")
```

Then replace both hardcoded URLs:
- Line 836: `http://ingestion:8081/ingest` → `f"{INGESTION_URL}/ingest"`
- Line 1133: `http://ingestion:8081/snapshots/create` → `f"{INGESTION_URL}/snapshots/create"`

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestMcpServerIngestionUrl -v`
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
git add mcp-server/server.py tests/test_audit_pii_protection.py
git commit -m "refactor: use INGESTION_URL env var instead of hardcoded URL"
```

---

### Task 3: MCP Server — PII-Aware `log_access()`

**Files:**
- Modify: `mcp-server/server.py` (the `log_access` function at line 199, and its call sites at lines 755 and 919)
- Test: `tests/test_audit_pii_protection.py` (add test class)

**Step 1: Write the failing test**

Add to `tests/test_audit_pii_protection.py`:

```python
class TestLogAccessPiiScanning(unittest.TestCase):
    """log_access must scan query text for PII before storing."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__),
                            "..", "mcp-server", "server.py")
        with open(path) as f:
            cls.source = f.read()

    def test_log_access_calls_scan_endpoint(self):
        """log_access must call the /scan endpoint for PII detection."""
        # Extract the log_access function body
        match = re.search(
            r'async def log_access\(.*?\n(?=\n(?:async def |class |# ──))',
            self.source, re.DOTALL
        )
        self.assertIsNotNone(match, "log_access function not found")
        func_body = match.group()
        self.assertIn("/scan", func_body,
                       "log_access must call the /scan endpoint")

    def test_log_access_sets_contains_pii(self):
        """log_access INSERT must include contains_pii column."""
        match = re.search(
            r'async def log_access\(.*?\n(?=\n(?:async def |class |# ──))',
            self.source, re.DOTALL
        )
        func_body = match.group()
        self.assertIn("contains_pii", func_body,
                       "log_access must set the contains_pii column")

    def test_log_access_stores_masked_query(self):
        """log_access must replace raw query with masked version."""
        match = re.search(
            r'async def log_access\(.*?\n(?=\n(?:async def |class |# ──))',
            self.source, re.DOTALL
        )
        func_body = match.group()
        self.assertIn("masked_text", func_body,
                       "log_access must use masked_text from scan result")

    def test_search_knowledge_no_raw_query_in_context(self):
        """search_knowledge log_access call must not pass raw query."""
        # Find the log_access call in search_knowledge handler
        # It should reference masked/scanned text, not the raw query variable
        search_section = self.source.split("# ── query_data")[0]
        log_calls = re.findall(r'await log_access\(.*?\)', search_section, re.DOTALL)
        for call in log_calls:
            if '"search"' in call and '"query"' in call:
                # The query value should NOT be the raw `query` variable directly
                self.assertNotRegex(call, r'"query":\s*query\b',
                    "log_access must not pass raw query variable — use masked text")

    def test_get_code_context_logs_query(self):
        """get_code_context must also log its query text (masked)."""
        # Find the get_code_context section
        code_section = self.source.split("# ── get_code_context")[1].split("# ──")[0] \
            if "# ── get_code_context" in self.source else ""
        if not code_section:
            code_section = self.source.split("get_code_context")[1].split("# ──")[0]
        log_calls = re.findall(r'await log_access\(.*?\)', code_section, re.DOTALL)
        self.assertTrue(len(log_calls) > 0, "get_code_context must call log_access")
        # At least one log_access call should include query context
        has_query = any("query" in call for call in log_calls)
        self.assertTrue(has_query,
            "get_code_context log_access must include query in context")
```

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestLogAccessPiiScanning -v`
Expected: FAIL — log_access doesn't call /scan, doesn't set contains_pii, get_code_context doesn't log query.

**Step 3: Write minimal implementation**

Modify `log_access()` in `mcp-server/server.py` (replacing lines 199-209):

```python
async def log_access(agent_id: str, agent_role: str,
                     resource_type: str, resource_id: str,
                     action: str, policy_result: str,
                     context: dict | None = None):
    contains_pii = False

    if context and "query" in context:
        # Scan query text for PII before storing
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

    pool = await get_pg_pool()
    await pool.execute("""
        INSERT INTO agent_access_log
            (agent_id, agent_role, resource_type, resource_id,
             action, policy_result, request_context, contains_pii)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    """, agent_id, agent_role, resource_type, resource_id,
       action, policy_result, json.dumps(context or {}), contains_pii)
```

Modify `get_code_context` log_access call (line 919) to include query:

```python
        await log_access(agent_id, agent_role, "code", "knowledge_code", "search", "allow", {
            "query": query, "qdrant_results": len(results.points),
            "after_policy": len(code_results), "after_rerank": len(reranked),
        })
```

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestLogAccessPiiScanning -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add mcp-server/server.py tests/test_audit_pii_protection.py
git commit -m "feat: scan query text for PII before writing audit log entries"
```

---

### Task 4: SQL Migration — RLS on `agent_access_log`

**Files:**
- Create: `init-db/008_audit_rls.sql`
- Test: `tests/test_audit_pii_protection.py` (add test class)

**Step 1: Write the failing test**

Add to `tests/test_audit_pii_protection.py`:

```python
class TestAuditRlsMigration(unittest.TestCase):
    """Migration 008 must enable RLS on agent_access_log."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__),
                            "..", "init-db", "008_audit_rls.sql")
        if os.path.exists(path):
            with open(path) as f:
                cls.source = f.read().lower()
        else:
            cls.source = ""

    def test_migration_file_exists(self):
        """008_audit_rls.sql must exist."""
        self.assertTrue(len(self.source) > 0, "008_audit_rls.sql does not exist or is empty")

    def test_enables_rls(self):
        """Must enable RLS on agent_access_log."""
        self.assertIn("enable row level security", self.source)
        self.assertIn("agent_access_log", self.source)

    def test_forces_rls(self):
        """Must FORCE RLS (even table owner cannot bypass)."""
        self.assertIn("force row level security", self.source)

    def test_insert_policy_for_app(self):
        """Must have INSERT policy for mcp_app role."""
        self.assertIn("for insert", self.source)
        self.assertIn("mcp_app", self.source)

    def test_select_policy_for_auditor(self):
        """Must have SELECT policy for mcp_auditor role."""
        self.assertIn("for select", self.source)
        self.assertIn("mcp_auditor", self.source)

    def test_creates_auditor_role(self):
        """Must create mcp_auditor role if not exists."""
        self.assertIn("mcp_auditor", self.source)
        self.assertIn("create role", self.source)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestAuditRlsMigration -v`
Expected: FAIL — file does not exist.

**Step 3: Write the migration**

Create `init-db/008_audit_rls.sql`:

```sql
-- ============================================================
-- 008_audit_rls.sql — Row-Level Security for Audit Logs
-- ============================================================
-- Secures agent_access_log so that:
--   - mcp_app can INSERT (write audit entries) but not read/modify
--   - mcp_auditor can SELECT (compliance/monitoring) but not modify
--   - No role can UPDATE or DELETE (append-only audit trail)
-- ============================================================

-- Create auditor role (for compliance / monitoring access)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_auditor') THEN
        CREATE ROLE mcp_auditor NOLOGIN;
    END IF;
END
$$;

-- Grant connect + usage so mcp_auditor can query the table
GRANT CONNECT ON DATABASE knowledgebase TO mcp_auditor;
GRANT USAGE ON SCHEMA public TO mcp_auditor;

-- Enable RLS
ALTER TABLE agent_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_access_log FORCE ROW LEVEL SECURITY;

-- Policy: mcp_app can only INSERT (write audit entries)
CREATE POLICY audit_insert_only ON agent_access_log
    FOR INSERT TO mcp_app
    WITH CHECK (true);

-- Policy: mcp_auditor can only SELECT (read for compliance)
CREATE POLICY audit_read_only ON agent_access_log
    FOR SELECT TO mcp_auditor
    USING (true);

-- Explicit: mcp_app gets INSERT, mcp_auditor gets SELECT
GRANT INSERT ON agent_access_log TO mcp_app;
GRANT SELECT ON agent_access_log TO mcp_auditor;

-- Ensure sequence access for mcp_app (BIGSERIAL needs it)
GRANT USAGE, SELECT ON SEQUENCE agent_access_log_id_seq TO mcp_app;
```

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestAuditRlsMigration -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add init-db/008_audit_rls.sql tests/test_audit_pii_protection.py
git commit -m "feat: add RLS migration for agent_access_log (008_audit_rls.sql)"
```

---

### Task 5: Retention — Time-Based Anonymization

**Files:**
- Modify: `ingestion/retention_cleanup.py` (add function, integrate into main)
- Test: `tests/test_audit_pii_protection.py` (add test class)

**Step 1: Write the failing test**

Add to `tests/test_audit_pii_protection.py`:

```python
class TestRetentionAnonymization(unittest.TestCase):
    """retention_cleanup.py must have time-based audit log anonymization."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__),
                            "..", "ingestion", "retention_cleanup.py")
        with open(path) as f:
            cls.source = f.read()

    def test_anonymize_function_exists(self):
        """anonymize_old_audit_logs function must exist."""
        self.assertIn("async def anonymize_old_audit_logs", self.source)

    def test_uses_retention_days_config(self):
        """Must use configurable retention days (env var or parameter)."""
        self.assertIn("AUDIT_RETENTION_DAYS", self.source)

    def test_anonymizes_request_context(self):
        """Must set request_context to anonymized marker."""
        self.assertIn('{"anonymized": true}', self.source)

    def test_time_based_filter(self):
        """Must filter by created_at age, not just by resource_id."""
        match = re.search(
            r'async def anonymize_old_audit_logs.*?(?=\nasync def |\nclass |\Z)',
            self.source, re.DOTALL
        )
        self.assertIsNotNone(match, "Function not found")
        func_body = match.group()
        self.assertIn("created_at", func_body)
        self.assertIn("interval", func_body)

    def test_called_in_main(self):
        """anonymize_old_audit_logs must be called from main()."""
        main_match = re.search(
            r'async def main\(.*?\Z', self.source, re.DOTALL
        )
        self.assertIsNotNone(main_match, "main() not found")
        self.assertIn("anonymize_old_audit_logs", main_match.group())
```

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestRetentionAnonymization -v`
Expected: FAIL — function does not exist.

**Step 3: Write minimal implementation**

In `ingestion/retention_cleanup.py`, add near the top (after line 28):

```python
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))
```

Add import `os` if not already present (it's not — add at line 17).

Add function before `main()`:

```python
async def anonymize_old_audit_logs(pool: asyncpg.Pool, execute: bool) -> dict:
    """
    Anonymisiert Audit-Log-Einträge, deren Aufbewahrungsfrist abgelaufen ist.
    Setzt request_context auf '{"anonymized": true}' für Einträge älter als
    AUDIT_RETENTION_DAYS Tage.
    """
    count = await pool.fetchval("""
        SELECT count(*) FROM agent_access_log
        WHERE created_at < now() - interval '1 day' * $1
          AND request_context != '{"anonymized": true}'::jsonb
    """, AUDIT_RETENTION_DAYS)

    report = {
        "retention_days": AUDIT_RETENTION_DAYS,
        "entries_to_anonymize": count,
        "actions": [],
    }

    if execute and count > 0:
        await pool.execute("""
            UPDATE agent_access_log
            SET request_context = '{"anonymized": true}'::jsonb
            WHERE created_at < now() - interval '1 day' * $1
              AND request_context != '{"anonymized": true}'::jsonb
        """, AUDIT_RETENTION_DAYS)
        report["actions"].append(f"Audit-Log: {count} Einträge anonymisiert (>{AUDIT_RETENTION_DAYS} Tage)")
    else:
        report["actions"].append(f"[DRY-RUN] Würde {count} Audit-Einträge anonymisieren (>{AUDIT_RETENTION_DAYS} Tage)")

    return report
```

Add to `main()` function (before `await pool.close()`):

```python
    # 4. Audit-Log: Zeitbasierte Anonymisierung
    log.info("=== Phase 4: Audit-Log Retention ===")
    audit_report = await anonymize_old_audit_logs(pool, args.execute)
    for action in audit_report["actions"]:
        log.info(f"  {action}")
```

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_audit_pii_protection.TestRetentionAnonymization -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add ingestion/retention_cleanup.py tests/test_audit_pii_protection.py
git commit -m "feat: add time-based audit log anonymization to retention cleanup"
```

---

### Task 6: Documentation — Update bekannte-schwachstellen.md

**Files:**
- Modify: `docs/bekannte-schwachstellen.md`

**Step 1: Mark the audit log PII gap as resolved**

Check `docs/bekannte-schwachstellen.md` for any mention of audit logs or PII in queries.
If there is no explicit entry, add one under the appropriate priority section documenting
what was done:

- PII scanning of query text before audit log storage
- RLS on agent_access_log (008_audit_rls.sql)
- Time-based retention anonymization
- Consistent logging for get_code_context

**Step 2: Commit**

```bash
git add docs/bekannte-schwachstellen.md
git commit -m "docs: document audit log PII protection as resolved"
```

---

### Task 7: Full Test Run

**Step 1: Run all tests**

Run: `python3 -m unittest discover -s tests -v`
Expected: All new tests pass. Pre-existing failure in `test_mcp_server_no_longer_uses_stdio_transport` is expected (not our code).

**Step 2: Verify test count**

The test suite should now have ~95+ tests (73 before + 22 new).
