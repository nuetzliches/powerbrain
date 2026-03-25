"""Tests for embed_text with mocked LLM provider HTTP calls."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import httpx

import server
from server import embed_text


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    """Patch the module-level http client for all tests."""
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    # Clear embedding cache to prevent cross-test contamination
    if hasattr(server, 'embedding_cache'):
        server.embedding_cache._cache.clear()
    return mock_client


class TestEmbedText:
    async def test_returns_embedding_vector(self, _patch_http):
        expected = [0.1, 0.2, 0.3]
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"data": [{"embedding": expected}]}
        _patch_http.post.return_value = response

        result = await embed_text("test query")

        assert result == expected
        _patch_http.post.assert_called_once()
        call_args = _patch_http.post.call_args
        assert "/v1/embeddings" in call_args[0][0]
        assert call_args[1]["json"]["model"] == "nomic-embed-text"
        assert call_args[1]["json"]["input"] == "test query"

    async def test_raises_on_http_error(self, _patch_http):
        response = MagicMock()
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        _patch_http.post.return_value = response

        with pytest.raises(httpx.HTTPStatusError):
            await embed_text("test query")

    async def test_retries_on_connect_error(self, _patch_http):
        """embed_text should retry on ConnectError (tenacity)."""
        response_ok = MagicMock()
        response_ok.raise_for_status = MagicMock()
        response_ok.json.return_value = {"data": [{"embedding": [0.1]}]}

        _patch_http.post.side_effect = [
            httpx.ConnectError("connection refused"),
            response_ok,
        ]

        # Patch tenacity wait to avoid real sleep
        with patch.object(embed_text.retry, "wait", return_value=0):
            result = await embed_text("retry test")

        assert result == [0.1]
        assert _patch_http.post.call_count == 2

    async def test_retries_on_timeout(self, _patch_http):
        """embed_text should retry on TimeoutException."""
        response_ok = MagicMock()
        response_ok.raise_for_status = MagicMock()
        response_ok.json.return_value = {"data": [{"embedding": [0.5]}]}

        _patch_http.post.side_effect = [
            httpx.TimeoutException("timeout"),
            response_ok,
        ]

        with patch.object(embed_text.retry, "wait", return_value=0):
            result = await embed_text("timeout test")

        assert result == [0.5]
