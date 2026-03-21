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
        sk_start = self.source.index('name == "search_knowledge"')
        sk_end = self.source.index('name == "get_code_context"', sk_start)
        section = self.source[sk_start:sk_end]

        self.assertIn("asyncio.gather", section,
                       "search_knowledge must use asyncio.gather for OPA checks")
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
