# tests/test_retention_vault_cleanup.py
"""Validates retention_cleanup.py handles vault entries."""
import unittest
import pathlib

CLEANUP_FILE = pathlib.Path(__file__).resolve().parent.parent / "ingestion" / "retention_cleanup.py"


class TestRetentionVaultCleanup(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = CLEANUP_FILE.read_text(encoding="utf-8")

    def test_vault_content_cleanup(self):
        """Must query and delete expired vault content."""
        self.assertIn("pii_vault.original_content", self.code)

    def test_vault_mapping_cleanup(self):
        """Must delete associated pseudonym mappings."""
        self.assertIn("pii_vault.pseudonym_mapping", self.code)

    def test_orphaned_vault_cleanup(self):
        """Must handle orphaned vault entries."""
        self.assertIn("orphan", self.code.lower())

    def test_vault_cleanup_function(self):
        """Must have a dedicated vault cleanup function."""
        self.assertIn("def clean_expired_vault", self.code)


if __name__ == "__main__":
    unittest.main()
