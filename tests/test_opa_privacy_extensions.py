# tests/test_opa_privacy_extensions.py
"""Validates OPA privacy.rego extensions for dual storage and vault access."""
import unittest
import pathlib

REGO_FILE = pathlib.Path(__file__).resolve().parent.parent / "opa-policies" / "kb" / "privacy.rego"


class TestOpaPrivacyExtensions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rego = REGO_FILE.read_text(encoding="utf-8")

    def test_dual_storage_default(self):
        self.assertIn("default dual_storage_enabled := false", self.rego)

    def test_dual_storage_rules_exist(self):
        self.assertIn("dual_storage_enabled", self.rego)
        self.assertIn('"internal"', self.rego)

    def test_vault_access_default(self):
        self.assertIn("default vault_access_allowed := false", self.rego)

    def test_vault_access_checks_purpose(self):
        self.assertIn("vault_access_allowed", self.rego)
        self.assertIn("token_valid", self.rego)

    def test_vault_fields_to_redact(self):
        self.assertIn("vault_fields_to_redact", self.rego)


if __name__ == "__main__":
    unittest.main()
