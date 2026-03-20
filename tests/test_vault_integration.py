# tests/test_vault_integration.py
"""
Integration smoke test for the Sealed Vault dual storage pipeline.
Requires running services (Qdrant, PostgreSQL, OPA, Ollama).
Run with: RUN_INTEGRATION_TESTS=1 pytest tests/test_vault_integration.py -v
"""
import unittest
import os

INTEGRATION = os.getenv("RUN_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes")


@unittest.skipUnless(INTEGRATION, "Set RUN_INTEGRATION_TESTS=1 to run")
class TestVaultIntegration(unittest.TestCase):
    """Smoke test: ingest PII data → verify vault + qdrant → search → verify pseudonymized."""

    async def test_ingest_with_pii_creates_vault_entry(self):
        """Ingest internal-classified data with PII → should create vault entry."""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://localhost:8081/ingest", json={
                "source": "integration-test-vault",
                "source_type": "csv",
                "project": "test-project",
                "classification": "internal",
                "content": "Max Mustermann, max@example.com, +49 170 1234567",
                "metadata": {"data_category": "customer_data"},
            })
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("pii_detected"))
            self.assertTrue(data.get("dual_storage"))

    async def test_search_returns_pseudonymized(self):
        """Search without token should return pseudonymized text."""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://localhost:8080/mcp", json={
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {
                        "query": "Mustermann",
                        "agent_id": "test-agent",
                        "agent_role": "analyst",
                    },
                },
            })
            self.assertEqual(resp.status_code, 200)
            # Results should NOT contain original names
            text = resp.text
            self.assertNotIn("Max Mustermann", text)
            self.assertNotIn("max@example.com", text)


if __name__ == "__main__":
    unittest.main()
