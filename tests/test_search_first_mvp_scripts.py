import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SearchFirstMvpScriptsTests(unittest.TestCase):
    def test_smoke_script_uses_host_network_path(self) -> None:
        script = (ROOT / "scripts" / "smoke_search_first_mvp.py").read_text()

        self.assertIn("--network", script)
        self.assertIn("host", script)
        self.assertNotIn('"docker",\n        "compose",\n        "exec"', script)


if __name__ == "__main__":
    unittest.main()
