"""Tests for rerank_results with mocked Reranker HTTP calls."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import server
from server import rerank_results


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


class TestRerankResults:
    @pytest.fixture
    def sample_docs(self):
        return [
            {"id": "a", "content": "Doc A", "score": 0.9, "metadata": {}},
            {"id": "b", "content": "Doc B", "score": 0.8, "metadata": {}},
            {"id": "c", "content": "Doc C", "score": 0.7, "metadata": {}},
        ]

    async def test_reranker_enabled(self, _patch_http, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "results": [
                {"id": "c", "original_score": 0.7, "rerank_score": 0.95,
                 "rank": 1, "content": "Doc C", "metadata": {}},
                {"id": "a", "original_score": 0.9, "rerank_score": 0.80,
                 "rank": 2, "content": "Doc A", "metadata": {}},
            ]
        }
        _patch_http.post.return_value = response

        result = await rerank_results("query", sample_docs, top_n=2)

        assert len(result) == 2
        assert result[0]["id"] == "c"
        assert "rerank_score" in result[0]

    async def test_reranker_disabled_returns_truncated(self, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", False)

        result = await rerank_results("query", sample_docs, top_n=2)
        assert len(result) == 2
        assert result[0]["id"] == "a"

    async def test_empty_docs_returns_empty(self, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        result = await rerank_results("query", [], top_n=5)
        assert result == []

    async def test_graceful_fallback_on_error(self, _patch_http, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        _patch_http.post.side_effect = Exception("Reranker down")

        result = await rerank_results("query", sample_docs, top_n=2)
        assert len(result) == 2
        assert result[0]["id"] == "a"

    async def test_graceful_fallback_on_http_error(self, _patch_http, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        response = MagicMock()
        response.raise_for_status.side_effect = Exception("500 error")
        _patch_http.post.return_value = response

        result = await rerank_results("query", sample_docs, top_n=2)
        assert len(result) == 2

    async def test_reranker_sends_correct_payload(self, _patch_http, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"results": []}
        _patch_http.post.return_value = response

        await rerank_results("my query", sample_docs, top_n=2)

        call_args = _patch_http.post.call_args
        assert "/rerank" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["query"] == "my query"
        assert payload["top_n"] == 2
        assert len(payload["documents"]) == 3
