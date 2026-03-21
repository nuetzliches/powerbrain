"""Tests for summarize_text and OPA summarization policy checks."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import server
from server import summarize_text, check_opa_summarization_policy


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


class TestSummarizeText:
    async def test_returns_summary(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "qwen2.5:3b")

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"response": "This is a summary."}
        _patch_http.post.return_value = response

        result = await summarize_text(
            chunks=["Chunk 1 content", "Chunk 2 content"],
            query="What is the topic?",
            detail="standard",
        )
        assert result == "This is a summary."

    async def test_sends_correct_payload(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "test-model")

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"response": "Summary"}
        _patch_http.post.return_value = response

        await summarize_text(
            chunks=["A", "B"],
            query="test query",
            detail="brief",
        )

        call_args = _patch_http.post.call_args
        assert "/api/generate" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["model"] == "test-model"
        assert "brief" in payload["system"].lower() or "concise" in payload["system"].lower()

    async def test_graceful_fallback_on_error(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "test-model")
        _patch_http.post.side_effect = Exception("Ollama down")

        result = await summarize_text(
            chunks=["A", "B"],
            query="test",
            detail="standard",
        )
        assert result is None

    async def test_empty_chunks_returns_none(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "test-model")

        result = await summarize_text(
            chunks=[],
            query="test",
            detail="standard",
        )
        assert result is None


class TestCheckOpaSummarizationPolicy:
    async def test_returns_policy_result(self, _patch_http):
        allowed_resp = MagicMock()
        allowed_resp.raise_for_status = MagicMock()
        allowed_resp.json.return_value = {"result": True}

        required_resp = MagicMock()
        required_resp.raise_for_status = MagicMock()
        required_resp.json.return_value = {"result": False}

        detail_resp = MagicMock()
        detail_resp.raise_for_status = MagicMock()
        detail_resp.json.return_value = {"result": "standard"}

        _patch_http.post.side_effect = [allowed_resp, required_resp, detail_resp]

        result = await check_opa_summarization_policy(
            agent_role="analyst",
            classification="internal",
        )
        assert result["allowed"] is True
        assert result["required"] is False
        assert result["detail"] == "standard"

    async def test_defaults_on_opa_failure(self, _patch_http):
        _patch_http.post.side_effect = Exception("OPA down")

        result = await check_opa_summarization_policy(
            agent_role="analyst",
            classification="internal",
        )
        assert result["allowed"] is False
        assert result["required"] is False
        assert result["detail"] == "standard"
