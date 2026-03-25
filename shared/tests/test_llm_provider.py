"""Tests for shared.llm_provider module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.llm_provider import (
    CompletionProvider,
    EmbeddingProvider,
    _BaseProvider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(*, status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"{status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


def _mock_http(**kwargs) -> AsyncMock:
    """Create an AsyncMock httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    resp = _mock_response(**kwargs)
    client.post.return_value = resp
    client.get.return_value = resp
    return client


# ===========================================================================
# EmbeddingProvider.__init__
# ===========================================================================

class TestEmbeddingProviderInit:
    def test_defaults(self):
        ep = EmbeddingProvider("http://localhost:11434")
        assert ep.base_url == "http://localhost:11434"
        assert ep.headers == {}

    def test_api_key_sets_auth_header(self):
        ep = EmbeddingProvider("http://host", api_key="sk-test")
        assert ep.headers == {"Authorization": "Bearer sk-test"}

    def test_trailing_slash_stripped(self):
        ep = EmbeddingProvider("http://host:1234/")
        assert ep.base_url == "http://host:1234"


# ===========================================================================
# EmbeddingProvider.embed
# ===========================================================================

class TestEmbeddingProviderEmbed:
    async def test_success(self):
        embedding = [0.1, 0.2, 0.3]
        http = _mock_http(json_data={"data": [{"embedding": embedding}]})
        ep = EmbeddingProvider("http://localhost:11434")

        result = await ep.embed(http, "hello world", "nomic-embed-text")

        assert result == embedding
        http.post.assert_called_once_with(
            "http://localhost:11434/v1/embeddings",
            headers={},
            json={"model": "nomic-embed-text", "input": "hello world"},
        )

    async def test_with_api_key(self):
        http = _mock_http(json_data={"data": [{"embedding": [1.0]}]})
        ep = EmbeddingProvider("http://host", api_key="sk-123")

        await ep.embed(http, "text", "model-x")

        http.post.assert_called_once_with(
            "http://host/v1/embeddings",
            headers={"Authorization": "Bearer sk-123"},
            json={"model": "model-x", "input": "text"},
        )

    async def test_http_error_propagates(self):
        http = _mock_http(status_code=500)
        ep = EmbeddingProvider("http://host")

        with pytest.raises(httpx.HTTPStatusError):
            await ep.embed(http, "text", "model")


# ===========================================================================
# CompletionProvider.__init__
# ===========================================================================

class TestCompletionProviderInit:
    def test_defaults(self):
        cp = CompletionProvider("http://localhost:11434")
        assert cp.base_url == "http://localhost:11434"
        assert cp.headers == {}


# ===========================================================================
# CompletionProvider.generate
# ===========================================================================

class TestCompletionProviderGenerate:
    async def test_success(self):
        json_data = {
            "choices": [{"message": {"content": "  Hello!  "}}],
        }
        http = _mock_http(json_data=json_data)
        cp = CompletionProvider("http://localhost:11434")

        result = await cp.generate(
            http,
            model="qwen2.5:3b",
            system_prompt="You are helpful.",
            user_prompt="Say hello",
        )

        assert result == "Hello!"
        http.post.assert_called_once_with(
            "http://localhost:11434/v1/chat/completions",
            headers={},
            json={
                "model": "qwen2.5:3b",
                "messages": [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Say hello"},
                ],
                "stream": False,
            },
        )

    async def test_empty_response_returns_none(self):
        json_data = {
            "choices": [{"message": {"content": "   "}}],
        }
        http = _mock_http(json_data=json_data)
        cp = CompletionProvider("http://host")

        result = await cp.generate(
            http, model="m", system_prompt="s", user_prompt="u"
        )

        assert result is None

    async def test_with_api_key(self):
        json_data = {
            "choices": [{"message": {"content": "ok"}}],
        }
        http = _mock_http(json_data=json_data)
        cp = CompletionProvider("http://host", api_key="key-42")

        await cp.generate(
            http, model="gpt-4", system_prompt="sys", user_prompt="usr"
        )

        http.post.assert_called_once_with(
            "http://host/v1/chat/completions",
            headers={"Authorization": "Bearer key-42"},
            json={
                "model": "gpt-4",
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "usr"},
                ],
                "stream": False,
            },
        )

    async def test_http_error_propagates(self):
        http = _mock_http(status_code=422)
        cp = CompletionProvider("http://host")

        with pytest.raises(httpx.HTTPStatusError):
            await cp.generate(
                http, model="m", system_prompt="s", user_prompt="u"
            )


# ===========================================================================
# _BaseProvider.health_check
# ===========================================================================

class TestHealthCheck:
    async def test_success_returns_true(self):
        http = _mock_http(status_code=200)
        provider = _BaseProvider("http://localhost:11434")

        assert await provider.health_check(http) is True
        http.get.assert_called_once_with(
            "http://localhost:11434/v1/models", headers={}
        )

    async def test_connection_error_returns_false(self):
        http = AsyncMock(spec=httpx.AsyncClient)
        http.get.side_effect = httpx.ConnectError("connection refused")
        provider = _BaseProvider("http://dead-host:9999")

        assert await provider.health_check(http) is False


# ===========================================================================
# EmbeddingProvider.embed_batch
# ===========================================================================

class TestEmbeddingProviderEmbedBatch:
    async def test_batch_success(self):
        embeddings = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        json_data = {
            "data": [
                {"index": 0, "embedding": embeddings[0]},
                {"index": 1, "embedding": embeddings[1]},
                {"index": 2, "embedding": embeddings[2]},
            ]
        }
        http = _mock_http(json_data=json_data)
        ep = EmbeddingProvider("http://localhost:11434")

        result = await ep.embed_batch(http, ["a", "b", "c"], "nomic-embed-text")

        assert result == embeddings
        http.post.assert_called_once_with(
            "http://localhost:11434/v1/embeddings",
            headers={},
            json={"model": "nomic-embed-text", "input": ["a", "b", "c"]},
        )

    async def test_batch_reorders_by_index(self):
        """OpenAI API may return results out of order."""
        json_data = {
            "data": [
                {"index": 2, "embedding": [0.5]},
                {"index": 0, "embedding": [0.1]},
                {"index": 1, "embedding": [0.3]},
            ]
        }
        http = _mock_http(json_data=json_data)
        ep = EmbeddingProvider("http://host")

        result = await ep.embed_batch(http, ["a", "b", "c"], "model")

        assert result == [[0.1], [0.3], [0.5]]

    async def test_batch_single_item(self):
        json_data = {"data": [{"index": 0, "embedding": [1.0, 2.0]}]}
        http = _mock_http(json_data=json_data)
        ep = EmbeddingProvider("http://host")

        result = await ep.embed_batch(http, ["text"], "model")

        assert result == [[1.0, 2.0]]

    async def test_batch_empty_list(self):
        ep = EmbeddingProvider("http://host")
        http = _mock_http()

        result = await ep.embed_batch(http, [], "model")

        assert result == []
        http.post.assert_not_called()

    async def test_batch_http_error_propagates(self):
        http = _mock_http(status_code=500)
        ep = EmbeddingProvider("http://host")

        with pytest.raises(httpx.HTTPStatusError):
            await ep.embed_batch(http, ["text"], "model")
