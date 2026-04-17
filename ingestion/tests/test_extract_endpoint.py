"""Integration-style tests for the POST /extract endpoint.

Uses FastAPI's TestClient which runs the handler synchronously; we patch the
shared ContentExtractor so these tests don't depend on markitdown being fully
functional inside the test environment.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Re-use the conftest.py sys.path tweak so `ingestion_api` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingestion_api  # noqa: E402


@pytest.fixture
def client():
    return TestClient(ingestion_api.app)


def _payload(data: bytes, filename: str, mime_type: str | None = None,
             max_bytes: int | None = None) -> dict:
    body = {
        "data": base64.b64encode(data).decode("ascii"),
        "filename": filename,
    }
    if mime_type is not None:
        body["mime_type"] = mime_type
    if max_bytes is not None:
        body["max_bytes"] = max_bytes
    return body


class TestExtractEndpointHappy:
    def test_text_file(self, client):
        resp = client.post("/extract", json=_payload(b"hello world", "note.txt"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == "hello world"
        assert body["extractor"] == "text"
        assert body["bytes_in"] == 11
        assert body["chars_out"] == 11
        assert body["content_type"] == "text"

    def test_markdown_file(self, client):
        resp = client.post("/extract", json=_payload(b"# Title", "page.md"))
        assert resp.status_code == 200
        assert resp.json()["text"] == "# Title"
        assert resp.json()["content_type"] == "markdown"

    def test_pdf_with_mocked_extractor(self, client):
        with patch.object(
            ingestion_api._content_extractor,
            "extract_from_bytes_detailed",
            return_value=("extracted pdf text", "markitdown"),
        ):
            resp = client.post(
                "/extract",
                json=_payload(b"fake pdf bytes", "report.pdf",
                              mime_type="application/pdf"),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "extracted pdf text"
        assert body["extractor"] == "markitdown"
        assert body["content_type"] == "markdown"


class TestExtractEndpointErrors:
    def test_invalid_base64(self, client):
        resp = client.post(
            "/extract",
            json={"data": "not-valid-base64!!!@#$", "filename": "x.txt"},
        )
        # FastAPI treats min_length=1 payloads differently; base64.b64decode
        # with validate=False is very permissive, so we may see 422 (empty
        # extract) or 400 (decode failure). Accept either as an error shape.
        assert resp.status_code in (400, 422)

    def test_oversize(self, client):
        big = b"x" * (ingestion_api.EXTRACT_MAX_BYTES + 100)
        resp = client.post("/extract", json=_payload(big, "huge.txt"))
        assert resp.status_code == 413
        detail = resp.json()["detail"].lower()
        assert "exceeds" in detail or "too large" in detail

    def test_request_level_cap(self, client):
        resp = client.post(
            "/extract",
            json=_payload(b"x" * 2_000, "note.txt", max_bytes=1_000),
        )
        assert resp.status_code == 413

    def test_skipped_mime(self, client):
        resp = client.post("/extract", json=_payload(b"\xff\xd8", "photo.jpg"))
        assert resp.status_code == 415

    def test_empty_extract(self, client):
        with patch.object(
            ingestion_api._content_extractor,
            "extract_from_bytes_detailed",
            return_value=(None, "failed"),
        ):
            resp = client.post(
                "/extract",
                json=_payload(b"x", "report.pdf", mime_type="application/pdf"),
            )
        assert resp.status_code == 422

    def test_whitespace_only_filename(self, client):
        """Filename that is all whitespace must be rejected (not passed through)."""
        resp = client.post(
            "/extract",
            json={
                "data": "aGVsbG8=",
                "filename": "   \t\n  ",
            },
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_encoded_payload_too_large(self, client):
        """A huge base64 string is rejected before we allocate the decoded bytes."""
        # Build a base64 string whose length alone exceeds the encoded cap.
        encoded_cap = (ingestion_api.EXTRACT_MAX_BYTES * 4) // 3 + 16
        oversize = "A" * (encoded_cap + 100)
        resp = client.post(
            "/extract",
            json={"data": oversize, "filename": "huge.txt"},
        )
        assert resp.status_code == 413
        assert "encoded payload" in resp.json()["detail"].lower()
