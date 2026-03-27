"""Tests for shared.rerank_provider module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.rerank_provider import (
    CohereRerankProvider,
    PowerbrainRerankProvider,
    RerankDocument,
    TEIRerankProvider,
    _BaseRerankProvider,
    create_rerank_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(*, status_code: int = 200, json_data: dict | list | None = None) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"{status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


def _mock_http(*, status_code: int = 200, json_data: dict | list | None = None) -> AsyncMock:
    """Create an AsyncMock httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    resp = _mock_response(status_code=status_code, json_data=json_data)
    client.post.return_value = resp
    client.get.return_value = resp
    return client


def _sample_docs(n: int = 3) -> list[RerankDocument]:
    """Create n sample RerankDocuments."""
    return [
        RerankDocument(
            id=f"doc-{i}",
            content=f"Content of document {i}",
            score=0.9 - i * 0.1,
            metadata={"source": f"test-{i}"},
        )
        for i in range(n)
    ]


# ===========================================================================
# RerankDocument
# ===========================================================================

class TestRerankDocument:
    def test_defaults(self):
        doc = RerankDocument(id="1", content="text")
        assert doc.score == 0.0
        assert doc.rerank_score == 0.0
        assert doc.rank == 0
        assert doc.metadata == {}

    def test_to_dict(self):
        doc = RerankDocument(id="1", content="text", score=0.5, rerank_score=0.8, rank=1, metadata={"k": "v"})
        d = doc.to_dict()
        assert d == {
            "id": "1",
            "content": "text",
            "score": 0.5,
            "rerank_score": 0.8,
            "rank": 1,
            "metadata": {"k": "v"},
        }


# ===========================================================================
# _BaseRerankProvider.__init__
# ===========================================================================

class TestBaseRerankProviderInit:
    def test_defaults(self):
        p = _BaseRerankProvider("http://localhost:8082")
        assert p.base_url == "http://localhost:8082"
        assert p.headers == {}
        assert p.model == ""

    def test_api_key_sets_auth_header(self):
        p = _BaseRerankProvider("http://host", api_key="sk-test")
        assert p.headers == {"Authorization": "Bearer sk-test"}

    def test_trailing_slash_stripped(self):
        p = _BaseRerankProvider("http://host:8082/")
        assert p.base_url == "http://host:8082"

    def test_model_stored(self):
        p = _BaseRerankProvider("http://host", model="rerank-v3.5")
        assert p.model == "rerank-v3.5"


# ===========================================================================
# _BaseRerankProvider.health_check
# ===========================================================================

class TestBaseHealthCheck:
    async def test_success_returns_true(self):
        http = _mock_http(status_code=200)
        provider = _BaseRerankProvider("http://localhost:8082")

        assert await provider.health_check(http) is True
        http.get.assert_called_once_with(
            "http://localhost:8082/health", headers={}
        )

    async def test_connection_error_returns_false(self):
        http = AsyncMock(spec=httpx.AsyncClient)
        http.get.side_effect = httpx.ConnectError("connection refused")
        provider = _BaseRerankProvider("http://dead-host:9999")

        assert await provider.health_check(http) is False

    async def test_non_200_returns_false(self):
        http = _mock_http(status_code=503)
        provider = _BaseRerankProvider("http://host")

        assert await provider.health_check(http) is False


# ===========================================================================
# PowerbrainRerankProvider
# ===========================================================================

class TestPowerbrainRerankProvider:
    async def test_sends_correct_payload(self):
        docs = _sample_docs(2)
        json_data = {
            "results": [
                {
                    "id": "doc-0", "content": "Content of document 0",
                    "original_score": 0.9, "rerank_score": 0.95,
                    "rank": 1, "metadata": {"source": "test-0"},
                },
                {
                    "id": "doc-1", "content": "Content of document 1",
                    "original_score": 0.8, "rerank_score": 0.85,
                    "rank": 2, "metadata": {"source": "test-1"},
                },
            ]
        }
        http = _mock_http(json_data=json_data)
        provider = PowerbrainRerankProvider("http://reranker:8082")

        results = await provider.rerank(http, "my query", docs, top_n=2)

        # Verify payload
        call_args = http.post.call_args
        assert call_args[0][0] == "http://reranker:8082/rerank"
        payload = call_args[1]["json"]
        assert payload["query"] == "my query"
        assert payload["top_n"] == 2
        assert payload["return_scores"] is True
        assert len(payload["documents"]) == 2
        assert payload["documents"][0]["id"] == "doc-0"
        assert payload["documents"][0]["content"] == "Content of document 0"
        assert payload["documents"][0]["score"] == 0.9
        assert payload["documents"][0]["metadata"] == {"source": "test-0"}

    async def test_response_mapping(self):
        docs = _sample_docs(2)
        json_data = {
            "results": [
                {
                    "id": "doc-1", "content": "Content of document 1",
                    "original_score": 0.8, "rerank_score": 0.95,
                    "rank": 1, "metadata": {"source": "test-1"},
                },
                {
                    "id": "doc-0", "content": "Content of document 0",
                    "original_score": 0.9, "rerank_score": 0.7,
                    "rank": 2, "metadata": {"source": "test-0"},
                },
            ]
        }
        http = _mock_http(json_data=json_data)
        provider = PowerbrainRerankProvider("http://reranker:8082")

        results = await provider.rerank(http, "query", docs, top_n=2)

        assert len(results) == 2
        assert results[0].id == "doc-1"
        assert results[0].rerank_score == 0.95
        assert results[0].rank == 1
        assert results[0].score == 0.8
        assert results[1].id == "doc-0"
        assert results[1].rank == 2

    async def test_empty_documents(self):
        http = _mock_http()
        provider = PowerbrainRerankProvider("http://reranker:8082")

        results = await provider.rerank(http, "query", [], top_n=5)

        assert results == []
        http.post.assert_not_called()

    async def test_http_error_propagates(self):
        http = _mock_http(status_code=500)
        provider = PowerbrainRerankProvider("http://reranker:8082")

        with pytest.raises(httpx.HTTPStatusError):
            await provider.rerank(http, "query", _sample_docs(1), top_n=1)

    async def test_with_api_key(self):
        json_data = {"results": []}
        http = _mock_http(json_data=json_data)
        provider = PowerbrainRerankProvider("http://host", api_key="key-42")

        await provider.rerank(http, "q", _sample_docs(1), top_n=1)

        call_args = http.post.call_args
        assert call_args[1]["headers"] == {"Authorization": "Bearer key-42"}


# ===========================================================================
# TEIRerankProvider
# ===========================================================================

class TestTEIRerankProvider:
    async def test_sends_texts_not_documents(self):
        docs = _sample_docs(3)
        json_data = [
            {"index": 0, "score": 0.5},
            {"index": 1, "score": 0.9},
            {"index": 2, "score": 0.3},
        ]
        http = _mock_http(json_data=json_data)
        provider = TEIRerankProvider("http://tei:8010")

        await provider.rerank(http, "my query", docs, top_n=2)

        call_args = http.post.call_args
        assert call_args[0][0] == "http://tei:8010/rerank"
        payload = call_args[1]["json"]
        assert payload["query"] == "my query"
        assert payload["texts"] == [
            "Content of document 0",
            "Content of document 1",
            "Content of document 2",
        ]
        assert payload["raw_scores"] is False
        assert payload["truncate"] is True
        # TEI does not support top_n in request
        assert "top_n" not in payload

    async def test_index_mapping_and_sort(self):
        docs = _sample_docs(3)
        # TEI returns all results; provider sorts and truncates
        json_data = [
            {"index": 0, "score": 0.5},
            {"index": 1, "score": 0.9},
            {"index": 2, "score": 0.3},
        ]
        http = _mock_http(json_data=json_data)
        provider = TEIRerankProvider("http://tei:8010")

        results = await provider.rerank(http, "query", docs, top_n=2)

        # Should be sorted by score desc, truncated to 2
        assert len(results) == 2
        assert results[0].id == "doc-1"  # index 1, score 0.9
        assert results[0].rerank_score == 0.9
        assert results[0].rank == 1
        assert results[0].score == 0.8  # original score of doc-1
        assert results[0].metadata == {"source": "test-1"}

        assert results[1].id == "doc-0"  # index 0, score 0.5
        assert results[1].rerank_score == 0.5
        assert results[1].rank == 2

    async def test_empty_documents(self):
        http = _mock_http()
        provider = TEIRerankProvider("http://tei:8010")

        results = await provider.rerank(http, "query", [], top_n=5)

        assert results == []
        http.post.assert_not_called()

    async def test_top_n_truncation(self):
        docs = _sample_docs(5)
        json_data = [
            {"index": i, "score": 1.0 - i * 0.1} for i in range(5)
        ]
        http = _mock_http(json_data=json_data)
        provider = TEIRerankProvider("http://tei:8010")

        results = await provider.rerank(http, "query", docs, top_n=3)

        assert len(results) == 3
        assert [r.rank for r in results] == [1, 2, 3]

    async def test_http_error_propagates(self):
        http = _mock_http(status_code=500)
        provider = TEIRerankProvider("http://tei:8010")

        with pytest.raises(httpx.HTTPStatusError):
            await provider.rerank(http, "query", _sample_docs(1), top_n=1)


# ===========================================================================
# CohereRerankProvider
# ===========================================================================

class TestCohereRerankProvider:
    async def test_sends_correct_payload_with_model(self):
        docs = _sample_docs(2)
        json_data = {
            "results": [
                {"index": 1, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.7},
            ]
        }
        http = _mock_http(json_data=json_data)
        provider = CohereRerankProvider(
            "https://api.cohere.com", api_key="co-key", model="rerank-v3.5"
        )

        await provider.rerank(http, "my query", docs, top_n=2)

        call_args = http.post.call_args
        assert call_args[0][0] == "https://api.cohere.com/v2/rerank"
        assert call_args[1]["headers"] == {"Authorization": "Bearer co-key"}
        payload = call_args[1]["json"]
        assert payload["model"] == "rerank-v3.5"
        assert payload["query"] == "my query"
        assert payload["documents"] == [
            "Content of document 0",
            "Content of document 1",
        ]
        assert payload["top_n"] == 2
        assert payload["return_documents"] is False

    async def test_relevance_score_mapping(self):
        docs = _sample_docs(3)
        json_data = {
            "results": [
                {"index": 2, "relevance_score": 0.98},
                {"index": 0, "relevance_score": 0.75},
            ]
        }
        http = _mock_http(json_data=json_data)
        provider = CohereRerankProvider("https://api.cohere.com", model="rerank-v3.5")

        results = await provider.rerank(http, "query", docs, top_n=2)

        assert len(results) == 2
        assert results[0].id == "doc-2"
        assert results[0].rerank_score == 0.98
        assert results[0].rank == 1
        assert results[0].score == 0.7  # original score of doc-2
        assert results[0].metadata == {"source": "test-2"}

        assert results[1].id == "doc-0"
        assert results[1].rerank_score == 0.75
        assert results[1].rank == 2

    async def test_empty_documents(self):
        http = _mock_http()
        provider = CohereRerankProvider("https://api.cohere.com", model="rerank-v3.5")

        results = await provider.rerank(http, "query", [], top_n=5)

        assert results == []
        http.post.assert_not_called()

    async def test_http_error_propagates(self):
        http = _mock_http(status_code=429)
        provider = CohereRerankProvider("https://api.cohere.com", model="rerank-v3.5")

        with pytest.raises(httpx.HTTPStatusError):
            await provider.rerank(http, "query", _sample_docs(1), top_n=1)


# ===========================================================================
# create_rerank_provider factory
# ===========================================================================

class TestCreateRerankProvider:
    def test_powerbrain_default(self):
        p = create_rerank_provider()
        assert isinstance(p, PowerbrainRerankProvider)
        assert p.base_url == "http://reranker:8082"

    def test_powerbrain_explicit(self):
        p = create_rerank_provider(backend="powerbrain", base_url="http://custom:9999")
        assert isinstance(p, PowerbrainRerankProvider)
        assert p.base_url == "http://custom:9999"

    def test_tei(self):
        p = create_rerank_provider(backend="tei", base_url="http://tei:8010")
        assert isinstance(p, TEIRerankProvider)
        assert p.base_url == "http://tei:8010"

    def test_cohere(self):
        p = create_rerank_provider(
            backend="cohere",
            base_url="https://api.cohere.com",
            api_key="co-key",
            model="rerank-v3.5",
        )
        assert isinstance(p, CohereRerankProvider)
        assert p.headers == {"Authorization": "Bearer co-key"}
        assert p.model == "rerank-v3.5"

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown reranker backend: 'magic'"):
            create_rerank_provider(backend="magic")
