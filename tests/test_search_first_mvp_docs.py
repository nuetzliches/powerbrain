import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SearchFirstMvpDocsTests(unittest.TestCase):
    def test_readme_mentions_seed_and_smoke_scripts(self) -> None:
        readme = (ROOT / "README.md").read_text()

        self.assertIn("scripts/seed_demo_search_data.py", readme)
        self.assertIn("scripts/smoke_search_first_mvp.py", readme)
        self.assertIn("MVP-Status", readme)

    def test_phase_two_boundary_is_documented(self) -> None:
        weaknesses = (ROOT / "docs" / "KNOWN_ISSUES.md").read_text()

        self.assertIn("Phase 2", weaknesses)
        self.assertIn("authentication", weaknesses.lower())


if __name__ == "__main__":
    unittest.main()
