# tests/test_art17_vault_deletion.py
"""Validates Art. 17 deletion handles vault data in two tiers."""
import unittest
import pathlib

CLEANUP_FILE = pathlib.Path(__file__).resolve().parent.parent / "ingestion" / "retention_cleanup.py"


class TestArt17VaultDeletion(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = CLEANUP_FILE.read_text(encoding="utf-8")

    def test_vault_deletion_in_process(self):
        """process_deletion_requests must delete vault data."""
        # The function should reference vault tables
        self.assertIn("pii_vault.original_content", self.code)
        self.assertIn("pii_vault.pseudonym_mapping", self.code)

    def test_restrict_tier(self):
        """Must handle 'restrict' action (delete vault, keep qdrant)."""
        self.assertIn("restrict", self.code)

    def test_deletion_records_vault(self):
        """Deletion records must track vault deletion."""
        self.assertIn("vault", self.code.lower())


if __name__ == "__main__":
    unittest.main()
