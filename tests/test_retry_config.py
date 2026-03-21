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

    def test_check_opa_retry_has_backoff(self):
        """check_opa_policy retry must use exponential backoff."""
        func_start = self.source.index("async def check_opa_policy")
        pre_func = self.source[max(0, func_start - 300):func_start]
        self.assertIn("wait_exponential", pre_func,
                       "check_opa_policy retry must use wait_exponential")

    def test_check_opa_retry_has_stop(self):
        """check_opa_policy retry must have a stop condition."""
        func_start = self.source.index("async def check_opa_policy")
        pre_func = self.source[max(0, func_start - 300):func_start]
        self.assertIn("stop_after_attempt", pre_func,
                       "check_opa_policy retry must use stop_after_attempt")


if __name__ == "__main__":
    unittest.main()
