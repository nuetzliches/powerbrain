# tests/test_pii_vault_schema.py
"""Validates pii_vault SQL migration structure."""
import unittest
import pathlib

SQL_FILE = pathlib.Path(__file__).resolve().parent.parent / "init-db" / "007_pii_vault.sql"


class TestPiiVaultSchema(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sql = SQL_FILE.read_text(encoding="utf-8").lower()

    def test_file_exists(self):
        self.assertTrue(SQL_FILE.exists(), "007_pii_vault.sql must exist")

    def test_creates_schema(self):
        self.assertIn("create schema", self.sql)
        self.assertIn("pii_vault", self.sql)

    def test_original_content_table(self):
        self.assertIn("pii_vault.original_content", self.sql)
        self.assertIn("original_text", self.sql)
        self.assertIn("pii_entities", self.sql)
        self.assertIn("retention_expires_at", self.sql)

    def test_pseudonym_mapping_table(self):
        self.assertIn("pii_vault.pseudonym_mapping", self.sql)
        self.assertIn("entity_type", self.sql)
        self.assertIn("salt", self.sql)

    def test_vault_access_log_table(self):
        self.assertIn("pii_vault.vault_access_log", self.sql)
        self.assertIn("token_hash", self.sql)
        self.assertIn("purpose", self.sql)

    def test_project_salts_table(self):
        self.assertIn("pii_vault.project_salts", self.sql)

    def test_rls_enabled(self):
        self.assertIn("row level security", self.sql)

    def test_vault_reader_role(self):
        self.assertIn("mcp_vault_reader", self.sql)


if __name__ == "__main__":
    unittest.main()
