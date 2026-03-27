"""
Integration tests for the reranker provider in the MCP server search pipeline.

Tests verify that:
- rerank_results() correctly uses the configured rerank provider
- Different backend types (powerbrain, tei, cohere) work through the abstraction
- Graceful fallback works when the provider raises errors
- The provider is swappable at runtime via create_rerank_provider()
"""

from unittest.mock import AsyncMock, MagicMock
import pytest

import server
from server import rerank_results
from shared.rerank_provider import (
    create_rerank_provider,
    RerankDocument,
    PowerbrainRerankProvider,
    TEIRerankProvider,
    CohereRerankProvider,
)


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


@pytest.fixture
def sample_docs():
    return [
        {"id": "a", "content": "Machine learning basics", "score": 0.9, "metadata": {"source": "doc1"}},
        {"id": "b", "content": "Deep learning advanced", "score": 0.8, "metadata": {"source": "doc2"}},
        {"id": "c", "content": "Neural network architectures", "score": 0.7, "metadata": {"source": "doc3"}},
        {"id": "d", "content": "Unrelated content about cooking", "score": 0.6, "metadata": {"source": "doc4"}},
    ]


def _mock_rerank_response(results: list[dict]) -> MagicMock:
    """Create a mock HTTP response for reranker."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"results": results}
    return response


class TestRerankerProviderIntegration:
    """Test that rerank_results uses the provider abstraction correctly."""

    async def test_powerbrain_provider_called(self, _patch_http, sample_docs, monkeypatch):
        """Verify PowerbrainReranker sends correct request format."""
        provider = create_rerank_provider(backend="powerbrain", base_url="http://reranker:8082")
        monkeypatch.setattr(server, "_rerank_provider", provider)
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        _patch_http.post.return_value = _mock_rerank_response([
            {"id": "c", "rerank_score": 0.95, "rank": 1, "content": "Neural network architectures", "original_score": 0.7, "metadata": {}},
            {"id": "a", "rerank_score": 0.85, "rank": 2, "content": "Machine learning basics", "original_score": 0.9, "metadata": {}},
        ])

        result = await rerank_results("neural networks", sample_docs, top_n=2)

        assert len(result) == 2
        assert result[0]["id"] == "c"
        assert result[0]["rerank_score"] == 0.95
        # Verify correct URL pattern
        call_url = _patch_http.post.call_args[0][0]
        assert "reranker:8082" in call_url
        assert "/rerank" in call_url

    async def test_tei_provider_called(self, _patch_http, sample_docs, monkeypatch):
        """Verify TEIReranker sends correct request format."""
        provider = create_rerank_provider(backend="tei", base_url="http://tei:8010")
        monkeypatch.setattr(server, "_rerank_provider", provider)
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        # TEI returns a different format — the provider translates it
        tei_response = MagicMock()
        tei_response.raise_for_status = MagicMock()
        tei_response.json.return_value = [
            {"index": 2, "score": 0.95},
            {"index": 0, "score": 0.85},
        ]
        _patch_http.post.return_value = tei_response

        result = await rerank_results("neural networks", sample_docs, top_n=2)

        assert len(result) == 2
        assert result[0]["id"] == "c"  # index 2 = doc "c"
        assert result[0]["rerank_score"] == 0.95
        # Verify TEI endpoint
        call_url = _patch_http.post.call_args[0][0]
        assert "tei:8010" in call_url
        assert "/rerank" in call_url

    async def test_cohere_provider_called(self, _patch_http, sample_docs, monkeypatch):
        """Verify CohereReranker sends correct request format."""
        provider = create_rerank_provider(
            backend="cohere", base_url="https://api.cohere.com",
            api_key="test-key", model="rerank-v3.5",
        )
        monkeypatch.setattr(server, "_rerank_provider", provider)
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        cohere_response = MagicMock()
        cohere_response.raise_for_status = MagicMock()
        cohere_response.json.return_value = {
            "results": [
                {"index": 2, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.85},
            ]
        }
        _patch_http.post.return_value = cohere_response

        result = await rerank_results("neural networks", sample_docs, top_n=2)

        assert len(result) == 2
        assert result[0]["id"] == "c"
        # Verify Cohere auth header
        call_kwargs = _patch_http.post.call_args[1]
        assert "Authorization" in call_kwargs.get("headers", {})

    async def test_provider_swap_at_runtime(self, _patch_http, sample_docs, monkeypatch):
        """Provider can be swapped without restarting the server."""
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        # Start with powerbrain
        pb_provider = create_rerank_provider(backend="powerbrain", base_url="http://reranker:8082")
        monkeypatch.setattr(server, "_rerank_provider", pb_provider)
        _patch_http.post.return_value = _mock_rerank_response([
            {"id": "a", "rerank_score": 0.9, "rank": 1, "content": "ML", "original_score": 0.9, "metadata": {}},
        ])
        result1 = await rerank_results("query", sample_docs, top_n=1)
        assert "reranker:8082" in _patch_http.post.call_args[0][0]

        # Swap to TEI
        tei_provider = create_rerank_provider(backend="tei", base_url="http://tei:8010")
        monkeypatch.setattr(server, "_rerank_provider", tei_provider)
        tei_resp = MagicMock()
        tei_resp.raise_for_status = MagicMock()
        tei_resp.json.return_value = [{"index": 0, "score": 0.9}]
        _patch_http.post.return_value = tei_resp
        result2 = await rerank_results("query", sample_docs, top_n=1)
        assert "tei:8010" in _patch_http.post.call_args[0][0]


class TestRerankerFallbackIntegration:
    """Test graceful degradation through the provider abstraction."""

    async def test_timeout_fallback(self, _patch_http, sample_docs, monkeypatch):
        """Provider timeout → graceful fallback to Qdrant ordering."""
        import httpx as httpx_mod
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        provider = create_rerank_provider(backend="powerbrain", base_url="http://reranker:8082")
        monkeypatch.setattr(server, "_rerank_provider", provider)

        _patch_http.post.side_effect = httpx_mod.TimeoutException("Timeout")

        result = await rerank_results("query", sample_docs, top_n=2)
        # Fallback: original Qdrant order, truncated
        assert len(result) == 2
        assert result[0]["id"] == "a"
        assert result[1]["id"] == "b"

    async def test_connection_error_fallback(self, _patch_http, sample_docs, monkeypatch):
        """Provider connection error → graceful fallback."""
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        provider = create_rerank_provider(backend="powerbrain", base_url="http://reranker:8082")
        monkeypatch.setattr(server, "_rerank_provider", provider)

        _patch_http.post.side_effect = ConnectionError("Connection refused")

        result = await rerank_results("query", sample_docs, top_n=2)
        assert len(result) == 2
        assert result[0]["id"] == "a"

    async def test_invalid_response_fallback(self, _patch_http, sample_docs, monkeypatch):
        """Provider returns garbage → graceful fallback."""
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        provider = create_rerank_provider(backend="powerbrain", base_url="http://reranker:8082")
        monkeypatch.setattr(server, "_rerank_provider", provider)

        response = MagicMock()
        response.raise_for_status.side_effect = Exception("500 Internal Server Error")
        _patch_http.post.return_value = response

        result = await rerank_results("query", sample_docs, top_n=2)
        assert len(result) == 2
        assert result[0]["id"] == "a"
