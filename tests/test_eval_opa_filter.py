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
        # search() must invoke OPA access check (directly or via helper)
        calls_opa = "kb/access" in search_body or "check_opa_access" in search_body
        self.assertTrue(calls_opa,
                        "search() must call OPA access policy (directly or via helper)")

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
