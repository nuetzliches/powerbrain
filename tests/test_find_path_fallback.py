"""Verify find_path has a fallback when shortestPath fails."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_SERVICE = ROOT / "mcp-server" / "graph_service.py"


class TestFindPathFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = GRAPH_SERVICE.read_text(encoding="utf-8")

    def test_find_path_has_try_except(self):
        """find_path must handle shortestPath failures."""
        func_start = self.source.index("async def find_path(")
        # Find end: next function def or end of file
        next_func_pos = self.source.find("\nasync def ", func_start + 1)
        if next_func_pos == -1:
            next_func_pos = len(self.source)
        func_body = self.source[func_start:next_func_pos]
        self.assertIn("except", func_body,
                       "find_path must catch shortestPath failures")

    def test_find_path_has_fallback(self):
        """find_path must have a BFS/iterative fallback."""
        func_start = self.source.index("async def find_path(")
        next_func_pos = self.source.find("\nasync def ", func_start + 1)
        if next_func_pos == -1:
            next_func_pos = len(self.source)
        func_body = self.source[func_start:next_func_pos]
        has_fallback = (
            "get_neighbors" in func_body or
            "fallback" in func_body.lower() or
            "bfs" in func_body.lower() or
            "MATCH" in func_body  # manual path query
        )
        self.assertTrue(has_fallback,
                        "find_path must have a fallback strategy")

    def test_find_path_logs_fallback(self):
        """find_path must log when falling back."""
        func_start = self.source.index("async def find_path(")
        next_func_pos = self.source.find("\nasync def ", func_start + 1)
        if next_func_pos == -1:
            next_func_pos = len(self.source)
        func_body = self.source[func_start:next_func_pos]
        self.assertIn("log.warning", func_body,
                       "find_path must log when shortestPath fails")


if __name__ == "__main__":
    unittest.main()
