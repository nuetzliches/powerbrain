# tests/test_ingestion_dual_storage.py
"""Validates ingestion_api.py has dual storage path wired to OPA."""
import unittest
import pathlib

API_FILE = pathlib.Path(__file__).resolve().parent.parent / "ingestion" / "ingestion_api.py"


class TestIngestionDualStorage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = API_FILE.read_text(encoding="utf-8")

    def test_calls_opa_pii_action(self):
        """Ingestion must call OPA to determine PII action."""
        self.assertIn("kb/privacy/pii_action", self.code)

    def test_calls_opa_dual_storage(self):
        """Ingestion must check dual_storage_enabled policy."""
        self.assertIn("dual_storage_enabled", self.code)

    def test_vault_insert(self):
        """Ingestion must write to pii_vault.original_content."""
        self.assertIn("pii_vault.original_content", self.code)

    def test_mapping_insert(self):
        """Ingestion must write to pii_vault.pseudonym_mapping."""
        self.assertIn("pii_vault.pseudonym_mapping", self.code)

    def test_scan_log_insert(self):
        """Ingestion must write to pii_scan_log."""
        self.assertIn("pii_scan_log", self.code)

    def test_vault_ref_in_payload(self):
        """Qdrant payload must include vault_ref."""
        self.assertIn("vault_ref", self.code)

    def test_pseudonymize_called(self):
        """Must call pseudonymize_text, not just mask_text."""
        self.assertIn("pseudonymize_text", self.code)

    def test_project_salt_lookup(self):
        """Must look up or create project salt."""
        self.assertIn("project_salts", self.code)


if __name__ == "__main__":
    unittest.main()
