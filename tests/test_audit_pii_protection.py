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
        lines = self.source.split('\n')
        for i, line in enumerate(lines, 1):
            if 'http.post(' in line and '"http://ingestion:8081' in line:
                self.fail(f"Line {i}: hardcoded ingestion URL in http.post call")


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
        """search_knowledge log_access call must not pass raw query directly."""
        search_section = self.source.split("# ── query_data")[0]
        log_calls = re.findall(r'await log_access\(.*?\)', search_section, re.DOTALL)
        for call in log_calls:
            if '"search"' in call and '"query"' in call:
                self.assertNotRegex(call, r'"query":\s*query\b',
                    "log_access must not pass raw query variable — use masked text")

    def test_get_code_context_logs_query(self):
        """get_code_context must also log its query text (masked)."""
        code_section = self.source.split("# ── get_code_context")[1].split("# ──")[0] \
            if "# ── get_code_context" in self.source else ""
        if not code_section:
            code_section = self.source.split("get_code_context")[1].split("# ──")[0]
        log_calls = re.findall(r'await log_access\(.*?\)', code_section, re.DOTALL)
        self.assertTrue(len(log_calls) > 0, "get_code_context must call log_access")
        has_query = any("query" in call for call in log_calls)
        self.assertTrue(has_query,
            "get_code_context log_access must include query in context")
