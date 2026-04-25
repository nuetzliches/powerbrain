"""E2E tests for chat-path document attachments (B-51).

Covers the full pipeline:

    client → pb-proxy → /extract on ingestion → PII pseudonymizer → LLM

Both attachment shapes are exercised:

* OpenAI ``{"type": "file"}`` block via ``POST /v1/chat/completions``
* Anthropic ``{"type": "document"}`` block via ``POST /v1/messages``

The "LLM saw plain text" promise is verified through Prometheus
counters rather than a mocked LLM:

* ``pbproxy_documents_extracted_total{status="ok",mime_type=...}`` —
  bumped iff the proxy successfully extracted the file before the LLM
  call.
* ``pbproxy_pii_entities_pseudonymized_total{entity_type=...}`` —
  bumped iff the pseudonymiser saw German PII in the *extracted* text
  (the source DOCX/PDF only carries the original name; the pseudonym
  comes from running PII on the post-extraction text).

Counter increments survive even when the upstream LLM call fails (no
GitHub PAT / no Ollama), so the assertions stay green on minimal
deployments while still proving the pipeline ran in the right order.

Prerequisites — start the full stack including pb-proxy:

    docker compose --profile proxy up -d
    RUN_INTEGRATION_TESTS=1 pytest tests/integration/e2e/test_document_attachment.py -v
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION_TESTS") != "1",
        reason="Set RUN_INTEGRATION_TESTS=1 to run E2E tests",
    ),
]

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8090")
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "testdata" / "documents"

# A valid 1×1 PNG (68 bytes) used to provoke a 415 — image/png is not on
# the proxy.documents allow-list. Embedding the bytes inline keeps the
# fixture self-contained.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000b49444154789c6360000200000500017a5eab3f0000"
    "000049454e44ae426082"
)


# ── Helpers ──────────────────────────────────────────────────


def _is_proxy_running() -> bool:
    try:
        return httpx.get(f"{PROXY_URL}/health", timeout=3).status_code == 200
    except httpx.ConnectError:
        return False


PROXY_NOT_RUNNING_REASON = (
    "Proxy service not running (start with: docker compose --profile proxy up -d)"
)


def _proxy_post(
    path: str,
    api_key: str,
    body: dict,
    timeout: float = 60,
) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    return httpx.post(
        f"{PROXY_URL}{path}", headers=headers, json=body, timeout=timeout,
    )


def _read_fixture_b64(name: str) -> str:
    return base64.b64encode((FIXTURES_DIR / name).read_bytes()).decode("ascii")


def _openai_file_block(filename: str, mime_type: str, data_b64: str) -> dict:
    """Build an OpenAI multimodal file block."""
    return {
        "type": "file",
        "file": {
            "file_data": f"data:{mime_type};base64,{data_b64}",
            "filename": filename,
        },
    }


def _anthropic_document_block(
    filename: str, mime_type: str, data_b64: str,
) -> dict:
    """Build an Anthropic multimodal document block."""
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": data_b64,
        },
        "title": filename,
    }


# ── Prometheus counter scraping ─────────────────────────────

_METRIC_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\{([^}]*)\}\s+(\S+)\s*$")


def _scrape_counter(name: str, **label_filter: str) -> float:
    """Return the value of a Prometheus counter line matching all labels.

    Returns 0.0 when the metric line doesn't yet exist (counters are
    created lazily on first ``.inc()``).
    """
    resp = httpx.get(f"{PROXY_URL}/metrics", timeout=5)
    resp.raise_for_status()
    total = 0.0
    for line in resp.text.splitlines():
        if line.startswith("#") or not line.startswith(name + "{"):
            continue
        m = _METRIC_RE.match(line)
        if not m or m.group(1) != name:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        if all(labels.get(k) == v for k, v in label_filter.items()):
            try:
                total += float(m.group(3))
            except ValueError:
                pass
    return total


# ── OpenAI shape — POST /v1/chat/completions ───────────────


@pytest.mark.skipif(
    not _is_proxy_running() if os.getenv("RUN_INTEGRATION_TESTS") == "1" else True,
    reason=PROXY_NOT_RUNNING_REASON,
)
class TestDocumentAttachmentOpenAI:
    """OpenAI ``file`` block via /v1/chat/completions."""

    def test_pdf_attachment_extracted_and_pii_pseudonymized(self, api_key):
        """A PDF with German PII is extracted to text and the PII
        pseudonymiser sees the extracted content before the LLM call.
        Verified via Prometheus counter increments — independent of
        whether the upstream LLM provider is reachable."""
        before_ok = _scrape_counter(
            "pbproxy_documents_extracted_total",
            status="ok", mime_type="application/pdf",
        )
        before_pii = _scrape_counter(
            "pbproxy_pii_entities_pseudonymized_total", entity_type="PERSON",
        )

        data_b64 = _read_fixture_b64("sample_with_pii.pdf")
        resp = _proxy_post(
            "/v1/chat/completions",
            api_key=api_key["key"],
            body={
                "model": "qwen-local",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Fasse das Dokument zusammen."},
                        _openai_file_block(
                            "sample_with_pii.pdf", "application/pdf", data_b64,
                        ),
                    ],
                }],
                "max_tokens": 50,
            },
            timeout=120,
        )
        # Auth + policy must pass (analyst is allowed). Anything from
        # 200 (LLM happy path) up to 5xx (LLM unreachable) is acceptable —
        # what matters is that extraction ran *before* the LLM call.
        assert resp.status_code != 401, resp.text
        assert resp.status_code != 403, resp.text

        after_ok = _scrape_counter(
            "pbproxy_documents_extracted_total",
            status="ok", mime_type="application/pdf",
        )
        after_pii = _scrape_counter(
            "pbproxy_pii_entities_pseudonymized_total", entity_type="PERSON",
        )
        assert after_ok > before_ok, "PDF was not extracted to text"
        assert after_pii > before_pii, (
            "PII pseudonymiser did not see PERSON in extracted text "
            "(extraction may have produced empty text or PII was missed)"
        )

    def test_docx_attachment_extracted(self, api_key):
        before_ok = _scrape_counter(
            "pbproxy_documents_extracted_total",
            status="ok",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        data_b64 = _read_fixture_b64("sample_with_pii.docx")
        resp = _proxy_post(
            "/v1/chat/completions",
            api_key=api_key["key"],
            body={
                "model": "qwen-local",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Was steht im Dokument?"},
                        _openai_file_block(
                            "sample_with_pii.docx",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            data_b64,
                        ),
                    ],
                }],
                "max_tokens": 50,
            },
            timeout=120,
        )
        assert resp.status_code not in (401, 403), resp.text
        after_ok = _scrape_counter(
            "pbproxy_documents_extracted_total",
            status="ok",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert after_ok > before_ok, "DOCX was not extracted to text"

    def test_oversize_attachment_returns_413(self, api_key):
        # Synthesise a payload larger than the policy cap (25 MB by
        # default). 26 MB of zero bytes compresses well in base64 but
        # the proxy compares the *decoded* size, so this trips the
        # 413 path in document_extraction.py.
        oversize_b64 = base64.b64encode(b"\x00" * (26 * 1024 * 1024)).decode()
        resp = _proxy_post(
            "/v1/chat/completions",
            api_key=api_key["key"],
            body={
                "model": "qwen-local",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Big file"},
                        _openai_file_block(
                            "big.pdf", "application/pdf", oversize_b64,
                        ),
                    ],
                }],
            },
            timeout=120,
        )
        assert resp.status_code == 413, resp.text
        assert "exceeds policy size cap" in resp.text or "size" in resp.text.lower()

    def test_disallowed_mime_returns_415(self, api_key):
        png_b64 = base64.b64encode(PNG_1X1).decode()
        resp = _proxy_post(
            "/v1/chat/completions",
            api_key=api_key["key"],
            body={
                "model": "qwen-local",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Image attached"},
                        _openai_file_block("tiny.png", "image/png", png_b64),
                    ],
                }],
            },
            timeout=30,
        )
        assert resp.status_code == 415, resp.text


# ── Anthropic shape — POST /v1/messages ─────────────────────


@pytest.mark.skipif(
    not _is_proxy_running() if os.getenv("RUN_INTEGRATION_TESTS") == "1" else True,
    reason=PROXY_NOT_RUNNING_REASON,
)
class TestDocumentAttachmentAnthropic:
    """Anthropic ``document`` block via /v1/messages.

    The Anthropic block is normalised to OpenAI ``file`` shape inside
    pb-proxy/anthropic_format.py before document_extraction runs, so
    these tests cover the conversion path on top of the same pipeline.
    """

    def test_pdf_via_messages_endpoint(self, api_key):
        before_ok = _scrape_counter(
            "pbproxy_documents_extracted_total",
            status="ok", mime_type="application/pdf",
        )
        data_b64 = _read_fixture_b64("sample_with_pii.pdf")
        resp = _proxy_post(
            "/v1/messages",
            api_key=api_key["key"],
            body={
                "model": "qwen-local",
                "max_tokens": 50,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Fasse zusammen."},
                        _anthropic_document_block(
                            "sample_with_pii.pdf", "application/pdf", data_b64,
                        ),
                    ],
                }],
            },
            timeout=120,
        )
        assert resp.status_code not in (401, 403), resp.text
        after_ok = _scrape_counter(
            "pbproxy_documents_extracted_total",
            status="ok", mime_type="application/pdf",
        )
        assert after_ok > before_ok, "PDF (Anthropic shape) was not extracted"

    def test_oversize_via_messages_returns_413(self, api_key):
        oversize_b64 = base64.b64encode(b"\x00" * (26 * 1024 * 1024)).decode()
        resp = _proxy_post(
            "/v1/messages",
            api_key=api_key["key"],
            body={
                "model": "qwen-local",
                "max_tokens": 50,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Big PDF"},
                        _anthropic_document_block(
                            "big.pdf", "application/pdf", oversize_b64,
                        ),
                    ],
                }],
            },
            timeout=120,
        )
        assert resp.status_code == 413, resp.text


# ── Auth / OPA gate ─────────────────────────────────────────


@pytest.mark.skipif(
    not _is_proxy_running() if os.getenv("RUN_INTEGRATION_TESTS") == "1" else True,
    reason=PROXY_NOT_RUNNING_REASON,
)
class TestDocumentAttachmentAuth:
    """Viewer role is denied at the proxy OPA gate before document
    extraction runs (`pb.proxy.allow` checks role membership)."""

    def test_viewer_with_attachment_returns_403(self, wait_for_services):
        from conftest import _create_api_key, _delete_api_key

        loop = asyncio.new_event_loop()
        viewer = loop.run_until_complete(_create_api_key("viewer"))

        try:
            data_b64 = _read_fixture_b64("sample_with_pii.pdf")
            resp = _proxy_post(
                "/v1/chat/completions",
                api_key=viewer["key"],
                body={
                    "model": "qwen-local",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Read this please"},
                            _openai_file_block(
                                "sample_with_pii.pdf",
                                "application/pdf",
                                data_b64,
                            ),
                        ],
                    }],
                },
                timeout=10,
            )
            # OPA proxy gate fires before document extraction — the viewer
            # never gets a chance to upload, regardless of attachment policy.
            assert resp.status_code == 403, resp.text
        finally:
            loop.run_until_complete(_delete_api_key(viewer["agent_id"]))
            loop.close()
