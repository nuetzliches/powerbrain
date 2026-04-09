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


class TestRerankContentEnrichment:
    """Tests for rerank_content metadata field and rerank_query support."""

    @pytest.fixture
    def docs_with_rerank_content(self):
        return [
            {"id": "a", "content": "Short text", "score": 0.9,
             "metadata": {"rerank_content": "Short text\n---\nfeat: add REST endpoint\nsrc/api/users.ts"}},
            {"id": "b", "content": "Another text", "score": 0.8, "metadata": {}},
        ]

    async def test_rerank_uses_rerank_content_from_metadata(
        self, _patch_http, docs_with_rerank_content, monkeypatch
    ):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"results": [
            {"id": "a", "original_score": 0.9, "rerank_score": 0.95,
             "rank": 1, "content": "enriched", "metadata": {}},
        ]}
        _patch_http.post.return_value = response

        await rerank_results("query", docs_with_rerank_content, top_n=1)

        payload = _patch_http.post.call_args[1]["json"]
        doc_a = next(d for d in payload["documents"] if d["id"] == "a")
        assert "feat: add REST endpoint" in doc_a["content"]
        assert "src/api/users.ts" in doc_a["content"]

    async def test_rerank_falls_back_to_content_when_no_rerank_content(
        self, _patch_http, docs_with_rerank_content, monkeypatch
    ):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"results": [
            {"id": "b", "original_score": 0.8, "rerank_score": 0.90,
             "rank": 1, "content": "Another text", "metadata": {}},
        ]}
        _patch_http.post.return_value = response

        await rerank_results("query", docs_with_rerank_content, top_n=1)

        payload = _patch_http.post.call_args[1]["json"]
        doc_b = next(d for d in payload["documents"] if d["id"] == "b")
        assert doc_b["content"] == "Another text"


class TestHeuristicBoosts:
    """Tests for _apply_heuristic_boosts and rerank_options."""

    @pytest.fixture
    def reranked_docs(self):
        from shared.rerank_provider import RerankDocument
        return [
            RerankDocument(id="a", content="Doc A", score=0.9, rerank_score=0.80,
                           rank=2, metadata={"project": "PROJ-1", "userName": "alice"}),
            RerankDocument(id="b", content="Doc B", score=0.8, rerank_score=0.85,
                           rank=1, metadata={"project": "PROJ-2", "userName": "bob",
                                             "files": ["src/api/users.ts", "src/models/user.ts"]}),
        ]

    def test_boost_same_project(self, reranked_docs):
        from server import _apply_heuristic_boosts
        options = {"match_project": "PROJ-1", "boost_same_project": 0.1}
        results = _apply_heuristic_boosts(reranked_docs, options)
        assert results[0].rerank_score == pytest.approx(0.90)  # 0.80 + 0.10
        assert results[1].rerank_score == pytest.approx(0.85)  # no match

    def test_boost_same_author(self, reranked_docs):
        from server import _apply_heuristic_boosts
        options = {"match_author": "bob", "boost_same_author": 0.05}
        results = _apply_heuristic_boosts(reranked_docs, options)
        assert results[0].rerank_score == pytest.approx(0.80)  # no match
        assert results[1].rerank_score == pytest.approx(0.90)  # 0.85 + 0.05

    def test_boost_file_overlap_partial(self, reranked_docs):
        from server import _apply_heuristic_boosts
        options = {
            "match_files": ["src/api/users.ts", "src/api/orders.ts"],
            "boost_file_overlap": 0.10,
        }
        results = _apply_heuristic_boosts(reranked_docs, options)
        # Doc b has 1/2 overlap → 0.10 * 0.5 = 0.05
        assert results[1].rerank_score == pytest.approx(0.90)  # 0.85 + 0.05
        # Doc a has no files → no boost
        assert results[0].rerank_score == pytest.approx(0.80)

    def test_heuristic_boost_reorders_results(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"results": [
            {"id": "winner", "original_score": 0.8, "rerank_score": 0.90,
             "rank": 1, "content": "Winner", "metadata": {"project": "OTHER"}},
            {"id": "boosted", "original_score": 0.7, "rerank_score": 0.80,
             "rank": 2, "content": "Boosted", "metadata": {"project": "MINE"}},
        ]}
        _patch_http.post.return_value = response

        docs = [
            {"id": "winner", "content": "Winner", "score": 0.8,
             "metadata": {"project": "OTHER"}},
            {"id": "boosted", "content": "Boosted", "score": 0.7,
             "metadata": {"project": "MINE"}},
        ]

        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            rerank_results("query", docs, top_n=2,
                           rerank_options={"match_project": "MINE",
                                           "boost_same_project": 0.15})
        )
        # boosted: 0.80 + 0.15 = 0.95 > winner: 0.90
        assert result[0]["id"] == "boosted"

    async def test_no_boosts_when_options_empty(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"results": [
            {"id": "a", "original_score": 0.9, "rerank_score": 0.95,
             "rank": 1, "content": "A", "metadata": {}},
        ]}
        _patch_http.post.return_value = response

        docs = [{"id": "a", "content": "A", "score": 0.9, "metadata": {}}]
        result = await rerank_results("query", docs, top_n=1, rerank_options=None)
        assert result[0]["rerank_score"] == pytest.approx(0.95)

    def test_boost_corrections(self, reranked_docs):
        from server import _apply_heuristic_boosts
        # Mark doc "a" as a correction
        reranked_docs[0].metadata["isCorrection"] = True
        options = {"boost_corrections": 0.15}
        results = _apply_heuristic_boosts(reranked_docs, options)
        assert results[0].rerank_score == pytest.approx(0.95)  # 0.80 + 0.15
        assert results[1].rerank_score == pytest.approx(0.85)  # not a correction

    def test_boost_corrections_no_effect_without_flag(self, reranked_docs):
        from server import _apply_heuristic_boosts
        # Neither doc has isCorrection
        options = {"boost_corrections": 0.15}
        results = _apply_heuristic_boosts(reranked_docs, options)
        assert results[0].rerank_score == pytest.approx(0.80)
        assert results[1].rerank_score == pytest.approx(0.85)

    def test_boost_corrections_zero_default(self, reranked_docs):
        from server import _apply_heuristic_boosts
        reranked_docs[0].metadata["isCorrection"] = True
        options = {}  # boost_corrections defaults to 0.0
        results = _apply_heuristic_boosts(reranked_docs, options)
        assert results[0].rerank_score == pytest.approx(0.80)  # no boost applied

    async def test_graceful_fallback_skips_boosts(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        _patch_http.post.side_effect = Exception("Reranker down")

        docs = [
            {"id": "a", "content": "A", "score": 0.9, "metadata": {"project": "P"}},
        ]
        result = await rerank_results(
            "query", docs, top_n=1,
            rerank_options={"match_project": "P", "boost_same_project": 0.1},
        )
        # Fallback returns original docs without boost
        assert result[0]["id"] == "a"
        assert "rerank_score" not in result[0]
