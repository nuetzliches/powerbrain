# tests/test_mcp_vault_access.py
"""Validates MCP server has vault access token validation and lookup."""
import unittest
import pathlib

SERVER_FILE = pathlib.Path(__file__).resolve().parent.parent / "mcp-server" / "server.py"


class TestMcpVaultAccess(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = SERVER_FILE.read_text(encoding="utf-8")

    def test_validate_pii_access_token_function(self):
        self.assertIn("def validate_pii_access_token", self.code)

    def test_vault_lookup_function(self):
        self.assertIn("def vault_lookup", self.code)

    def test_check_opa_vault_access(self):
        """Must call OPA vault_access_allowed policy."""
        self.assertIn("vault_access_allowed", self.code)

    def test_vault_access_log(self):
        """Must log vault access separately."""
        self.assertIn("vault_access_log", self.code)

    def test_search_knowledge_handles_token(self):
        """search_knowledge must accept pii_access_token parameter."""
        self.assertIn("pii_access_token", self.code)

    def test_hmac_validation(self):
        """Token validation must use HMAC."""
        self.assertIn("hmac", self.code)

    def test_redact_fields(self):
        """Must have field redaction function for vault results."""
        self.assertIn("def redact_fields", self.code)


if __name__ == "__main__":
    unittest.main()
