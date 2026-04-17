"""Unit tests for pb-proxy/document_extraction.py."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from document_extraction import (
    DocumentExtractionError,
    _extract_data_url,
    extract_documents_in_messages,
)


# ── Helpers ──────────────────────────────────────────────────


def _data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _file_message(mime: str, payload: bytes, filename: str = "doc.pdf") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Please read this:"},
            {
                "type": "file",
                "file": {"file_data": _data_url(mime, payload), "filename": filename},
            },
        ],
    }


def _policy(
    allowed: bool = True,
    max_bytes: int = 25_000_000,
    mimes: list[str] | None = None,
    max_files: int = 3,
) -> dict:
    return {
        "documents_allowed": allowed,
        "documents_max_bytes": max_bytes,
        "documents_allowed_mime_types": mimes
        or [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ],
        "documents_max_files": max_files,
    }


def _mock_http_client(status: int = 200, body: dict | None = None) -> MagicMock:
    body = body or {
        "text": "extracted text",
        "content_type": "markdown",
        "extractor": "markitdown",
        "bytes_in": 10,
        "chars_out": 14,
        "truncated": False,
    }
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body
    resp.text = "mock"

    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=resp)
    return client


# ── Tests ────────────────────────────────────────────────────


class TestDataUrl:
    def test_valid_data_url(self):
        out = _extract_data_url(_data_url("application/pdf", b"hello"))
        assert out is not None
        mime, raw = out
        assert mime == "application/pdf"
        assert raw == b"hello"

    def test_invalid_data_url(self):
        assert _extract_data_url("not a data url") is None


class TestExtractDocumentsInMessages:
    @pytest.mark.asyncio
    async def test_noop_when_no_files(self):
        client = _mock_http_client()
        messages = [{"role": "user", "content": "no attachments"}]
        out, result = await extract_documents_in_messages(messages, client, _policy())
        assert out == messages
        assert result.files == 0
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_extracts_pdf_successfully(self):
        client = _mock_http_client()
        msg = _file_message("application/pdf", b"pdf bytes")
        out, result = await extract_documents_in_messages([msg], client, _policy())

        assert result.files == 1
        assert result.bytes_in_total == len(b"pdf bytes")
        assert result.chars_out_total == len("extracted text")
        assert result.mime_types == ["application/pdf"]

        content = out[0]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "Please read this:"
        assert content[1]["type"] == "text"
        assert content[1]["text"].startswith("<File: doc.pdf>")
        assert "extracted text" in content[1]["text"]

    @pytest.mark.asyncio
    async def test_silently_drops_when_policy_denies(self):
        client = _mock_http_client()
        msg = _file_message("application/pdf", b"pdf bytes")
        policy = _policy(allowed=False)
        out, result = await extract_documents_in_messages([msg], client, policy)

        assert result.files == 0
        client.post.assert_not_called()
        replaced = out[0]["content"][1]
        assert replaced["type"] == "text"
        assert "removed" in replaced["text"].lower()

    @pytest.mark.asyncio
    async def test_raises_on_oversize(self):
        client = _mock_http_client()
        big = b"x" * 2_000
        msg = _file_message("application/pdf", big)
        policy = _policy(max_bytes=1_000)

        with pytest.raises(DocumentExtractionError) as exc_info:
            await extract_documents_in_messages([msg], client, policy)

        assert exc_info.value.status_code == 413

    @pytest.mark.asyncio
    async def test_raises_on_disallowed_mime(self):
        client = _mock_http_client()
        msg = _file_message("image/png", b"fake png")
        policy = _policy(mimes=["application/pdf"])

        with pytest.raises(DocumentExtractionError) as exc_info:
            await extract_documents_in_messages([msg], client, policy)

        assert exc_info.value.status_code == 415

    @pytest.mark.asyncio
    async def test_raises_on_too_many_files(self):
        client = _mock_http_client()
        msgs = [_file_message("application/pdf", b"x") for _ in range(5)]
        policy = _policy(max_files=2)

        with pytest.raises(DocumentExtractionError) as exc_info:
            await extract_documents_in_messages(msgs, client, policy)

        assert exc_info.value.status_code == 413

    @pytest.mark.asyncio
    async def test_propagates_ingestion_error(self):
        client = _mock_http_client(status=422, body={"detail": "empty"})
        msg = _file_message("application/pdf", b"pdf")

        with pytest.raises(DocumentExtractionError) as exc_info:
            await extract_documents_in_messages([msg], client, _policy())

        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_handles_ingestion_timeout(self):
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        msg = _file_message("application/pdf", b"pdf")

        with pytest.raises(DocumentExtractionError) as exc_info:
            await extract_documents_in_messages([msg], client, _policy())

        assert exc_info.value.status_code == 504
        # Error must carry mime-type hint for metrics labeling
        assert exc_info.value.mime_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_error_carries_mime_hint_for_oversize(self):
        client = _mock_http_client()
        big = b"x" * 5_000
        msg = _file_message("application/pdf", big)
        policy = _policy(max_bytes=1_000)

        with pytest.raises(DocumentExtractionError) as exc_info:
            await extract_documents_in_messages([msg], client, policy)

        assert exc_info.value.status_code == 413
        assert exc_info.value.mime_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_multiple_attachments_in_single_request(self):
        """Two PDFs in one user message are both extracted."""
        client = _mock_http_client()
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "compare these:"},
                {
                    "type": "file",
                    "file": {
                        "file_data": _data_url("application/pdf", b"one"),
                        "filename": "a.pdf",
                    },
                },
                {
                    "type": "file",
                    "file": {
                        "file_data": _data_url("application/pdf", b"two"),
                        "filename": "b.pdf",
                    },
                },
            ],
        }
        out, result = await extract_documents_in_messages([msg], client, _policy())

        assert result.files == 2
        assert result.filenames == ["a.pdf", "b.pdf"]
        content = out[0]["content"]
        assert content[1]["text"].startswith("<File: a.pdf>")
        assert content[2]["text"].startswith("<File: b.pdf>")
        assert client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_input_file_block_variant(self):
        """The OpenAI `input_file` block shape is recognized alongside `file`."""
        client = _mock_http_client()
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "input_file": {
                        "file_data": _data_url("application/pdf", b"alt"),
                        "filename": "alt.pdf",
                    },
                },
            ],
        }
        out, result = await extract_documents_in_messages([msg], client, _policy())
        assert result.files == 1
        assert out[0]["content"][0]["type"] == "text"
