"""Verify ingestion pipeline cleanup and chunk API."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INGESTION_FILE = ROOT / "ingestion" / "ingestion_api.py"
SERVER_FILE = ROOT / "mcp-server" / "server.py"


class TestIngestionCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingestion_src = INGESTION_FILE.read_text(encoding="utf-8")
        cls.server_src = SERVER_FILE.read_text(encoding="utf-8")

    def test_no_git_repo_stub(self):
        """git_repo stub must be removed from ingestion."""
        self.assertNotIn('"git_repo"', self.ingestion_src,
                          "git_repo stub must be removed from ingestion_api.py")

    def test_no_sql_dump_stub(self):
        """sql_dump stub must be removed from ingestion."""
        self.assertNotIn('"sql_dump"', self.ingestion_src,
                          "sql_dump stub must be removed from ingestion_api.py")

    def test_mcp_schema_text_only(self):
        """MCP ingest_data tool must only accept 'text' source_type."""
        # Find the ingest_data tool definition
        tool_start = self.server_src.index('"ingest_data"')
        # Find the end of this tool definition (next Tool( or end of list)
        tool_end = self.server_src.index("Tool(", tool_start + 1) if \
            "Tool(" in self.server_src[tool_start + 1:] else \
            self.server_src.index("]", tool_start)
        tool_def = self.server_src[tool_start:tool_end]
        self.assertNotIn("git_repo", tool_def,
                          "ingest_data schema must not list git_repo")
        self.assertNotIn("sql_dump", tool_def,
                          "ingest_data schema must not list sql_dump")

    def test_chunk_ingest_endpoint_exists(self):
        """/ingest/chunks endpoint must exist for adapter ingestion."""
        self.assertIn("/ingest/chunks", self.ingestion_src,
                       "Must have /ingest/chunks endpoint")

    def test_chunk_ingest_request_model(self):
        """ChunkIngestRequest model must exist with required fields."""
        self.assertIn("class ChunkIngestRequest", self.ingestion_src,
                       "Must define ChunkIngestRequest model")
        self.assertIn("chunks", self.ingestion_src,
                       "ChunkIngestRequest must have chunks field")

    def test_chunk_endpoint_calls_pipeline(self):
        """/ingest/chunks must call ingest_text_chunks for privacy pipeline."""
        # Find the chunks endpoint handler
        endpoint_start = self.ingestion_src.index("/ingest/chunks")
        endpoint_body = self.ingestion_src[endpoint_start:endpoint_start + 500]
        self.assertIn("ingest_text_chunks", endpoint_body,
                       "/ingest/chunks must call ingest_text_chunks")


if __name__ == "__main__":
    unittest.main()
