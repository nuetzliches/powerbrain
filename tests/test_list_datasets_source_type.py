import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestListDatasetsSourceType(unittest.TestCase):
    """Regression test for list_datasets 'source_type column does not exist' error.

    Root cause: The datasets table in 001_schema.sql defines source_type,
    but databases created before that column was added never got it.
    A migration (009_add_source_type.sql) must ALTER TABLE to add it.
    """

    def test_migration_file_exists(self) -> None:
        """009_add_source_type.sql must exist."""
        path = ROOT / "init-db" / "009_add_source_type.sql"
        self.assertTrue(path.exists(), f"{path} does not exist")

    def test_migration_adds_source_type_to_datasets(self) -> None:
        """Migration must ALTER TABLE datasets ADD COLUMN source_type."""
        sql = (ROOT / "init-db" / "009_add_source_type.sql").read_text().lower()
        self.assertIn("alter table", sql)
        self.assertIn("datasets", sql)
        self.assertIn("source_type", sql)
        self.assertIn("if not exists", sql,
                       "Must use IF NOT EXISTS to be idempotent")

    def test_migration_adds_source_type_to_documents_meta(self) -> None:
        """documents_meta also has source_type in the schema but may be missing."""
        sql = (ROOT / "init-db" / "009_add_source_type.sql").read_text().lower()
        self.assertIn("documents_meta", sql,
                       "Must also add source_type to documents_meta if missing")

    def test_list_datasets_query_matches_schema(self) -> None:
        """server.py list_datasets SELECT must only reference columns that exist."""
        server = (ROOT / "mcp-server" / "server.py").read_text()
        # The SELECT query for list_datasets must include source_type
        self.assertIn("source_type", server,
                       "list_datasets query must include source_type")


if __name__ == "__main__":
    unittest.main()
