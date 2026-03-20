import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class McpRequirementsTests(unittest.TestCase):
    def test_httpx_opentelemetry_requirement_uses_available_beta_version(self) -> None:
        requirements = (ROOT / "mcp-server" / "requirements.txt").read_text()

        self.assertIn("opentelemetry-instrumentation-httpx>=0.48b0", requirements)


if __name__ == "__main__":
    unittest.main()
