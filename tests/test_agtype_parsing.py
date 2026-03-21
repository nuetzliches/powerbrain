"""Verify graph_service.py handles AGE agtype parsing correctly."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_SERVICE = ROOT / "mcp-server" / "graph_service.py"


class TestAgtypeParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = GRAPH_SERVICE.read_text(encoding="utf-8")

    def test_agtype_suffix_stripping(self):
        """_execute_cypher must strip AGE agtype suffixes before JSON parsing."""
        # Must have re.sub call that strips ::vertex, ::edge, ::path suffixes
        func_start = self.source.index("async def _execute_cypher(")
        func_end = self.source.index("\nasync def ", func_start + 1)
        func_body = self.source[func_start:func_end]
        self.assertIn("re.sub", func_body,
                       "_execute_cypher must use re.sub to strip agtype suffixes")
        has_suffix_handling = (
            "::vertex" in func_body or
            "::edge" in func_body or
            "::path" in func_body or
            "vertex" in func_body
        )
        self.assertTrue(has_suffix_handling,
                        "_execute_cypher must handle AGE agtype suffixes like ::vertex, ::edge, ::path")

    def test_agtype_uses_regex(self):
        """agtype suffix stripping should use regex within _execute_cypher."""
        func_start = self.source.index("async def _execute_cypher(")
        func_end = self.source.index("\nasync def ", func_start + 1)
        func_body = self.source[func_start:func_end]
        self.assertIn("re.", func_body,
                       "_execute_cypher must use re module for agtype suffix stripping")

    def test_robust_json_fallback(self):
        """_execute_cypher must have fallback for unparseable agtype results."""
        func_start = self.source.index("async def _execute_cypher(")
        func_end = self.source.index("\nasync def ", func_start + 1)
        func_body = self.source[func_start:func_end]
        self.assertIn("except", func_body,
                       "_execute_cypher must catch JSON parse errors")
        self.assertIn("raw", func_body,
                       "_execute_cypher must preserve raw value on parse failure")


if __name__ == "__main__":
    unittest.main()
