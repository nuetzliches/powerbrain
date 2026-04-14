"""
Load test for Powerbrain MCP server search pipeline.

Usage:
    locust -f tests/load/locustfile.py --host=http://localhost:8080

Requires a running stack: docker compose --profile local-llm --profile local-reranker up -d

Web UI: http://localhost:8089
"""

import uuid
from locust import HttpUser, task, between


SEARCH_QUERIES = [
    "GDPR data retention policy",
    "How to handle personal data deletion requests",
    "What are the access control rules for confidential data",
    "Code review guidelines for security",
    "Employee onboarding process documentation",
]


class MCPSearchUser(HttpUser):
    """Simulates an MCP client performing search operations."""

    wait_time = between(1, 3)

    @task(3)
    def search_knowledge(self):
        """Search the knowledge base (most common operation)."""
        query = SEARCH_QUERIES[hash(uuid.uuid4()) % len(SEARCH_QUERIES)]
        self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {"query": query, "top_k": 5},
                },
            },
            headers={"Content-Type": "application/json"},
        )

    @task(1)
    def search_with_summarization(self):
        """Search with summarization enabled (heavier operation)."""
        self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {
                        "query": "What is our data classification policy?",
                        "top_k": 3,
                        "summarize": True,
                        "summary_detail": "brief",
                    },
                },
            },
            headers={"Content-Type": "application/json"},
        )

    @task(1)
    def list_datasets(self):
        """List available datasets (lightweight operation)."""
        self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {
                    "name": "list_datasets",
                    "arguments": {},
                },
            },
            headers={"Content-Type": "application/json"},
        )

    @task(1)
    def health_check(self):
        """Health endpoint (baseline latency)."""
        self.client.get("/health")
