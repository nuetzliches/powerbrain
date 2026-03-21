"""Verify graph_sync_log table is defined in migrations."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_DIR = ROOT / "init-db"
GRAPH_SERVICE = ROOT / "mcp-server" / "graph_service.py"


class TestGraphSyncLog(unittest.TestCase):
    def test_graph_sync_log_migration_exists(self):
        """A migration must define the graph_sync_log table."""
        found = False
        for sql_file in sorted(MIGRATION_DIR.glob("*.sql")):
            content = sql_file.read_text(encoding="utf-8")
            if "graph_sync_log" in content and "CREATE TABLE" in content:
                found = True
                break
        self.assertTrue(found,
                        "No migration creates the graph_sync_log table")

    def test_graph_sync_log_has_required_columns(self):
        """graph_sync_log must have entity_type, entity_id, action columns."""
        for sql_file in sorted(MIGRATION_DIR.glob("*.sql")):
            content = sql_file.read_text(encoding="utf-8")
            if "graph_sync_log" in content and "CREATE TABLE" in content:
                self.assertIn("entity_type", content,
                              "graph_sync_log must have entity_type column")
                self.assertIn("entity_id", content,
                              "graph_sync_log must have entity_id column")
                self.assertIn("action", content,
                              "graph_sync_log must have action column")
                return
        self.fail("No migration with graph_sync_log found")

    def test_graph_service_references_graph_sync_log(self):
        """graph_service.py must use graph_sync_log for mutation logging."""
        source = GRAPH_SERVICE.read_text(encoding="utf-8")
        self.assertIn("graph_sync_log", source,
                       "graph_service.py must reference graph_sync_log")


if __name__ == "__main__":
    unittest.main()
