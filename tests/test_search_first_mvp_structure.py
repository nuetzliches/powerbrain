import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SearchFirstMvpStructureTests(unittest.TestCase):
    def test_mcp_server_no_longer_uses_stdio_transport(self) -> None:
        server_source = (ROOT / "mcp-server" / "server.py").read_text()

        self.assertNotIn("from mcp.server.stdio import stdio_server", server_source)
        self.assertIn("streamable_http_app", server_source)
        self.assertIn("uvicorn.run", server_source)

    def test_mcp_dockerfile_copies_graph_service(self) -> None:
        dockerfile = (ROOT / "mcp-server" / "Dockerfile").read_text()

        self.assertIn("graph_service.py", dockerfile)

    def test_compose_mounts_local_opa_policies(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertIn("./opa-policies:/policies", compose)
        self.assertIn('      - "/policies"', compose)

    def test_mcp_server_does_not_require_reranker_for_startup(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertNotIn("reranker:\n        condition: service_healthy", compose)


if __name__ == "__main__":
    unittest.main()
