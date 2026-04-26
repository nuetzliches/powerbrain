"""Document-attachment middleware for the pb-proxy chat path.

Finds file/document blocks in OpenAI-style multimodal `messages[].content`
arrays, calls the ingestion service's ``POST /extract`` endpoint to convert
each attachment to text, and rewrites the block as a ``text`` part so the
downstream PII pseudonymizer and the LLM see plain text.

Supported block shapes (Anthropic `document` is pre-normalized to OpenAI
`file` by ``anthropic_format.py`` before this middleware runs):

    {"type": "file",       "file":       {"file_data": "data:<mime>;base64,...", "filename": "..."}}
    {"type": "input_file", "input_file": {"file_data": "data:<mime>;base64,...", "filename": "..."}}

This middleware is intentionally policy-aware:
  * allowed role (OPA ``pb.proxy.documents_allowed``)
  * allowed MIME types (OPA ``pb.proxy.documents_allowed_mime_types``)
  * per-file size cap (OPA ``pb.proxy.documents_max_bytes``)
  * per-request file count (OPA ``pb.proxy.documents_max_files``)

If the user is not allowed to attach documents, the blocks are silently
removed (letting the request through text-only), which mirrors the behavior
of ``filter_non_text_content()`` for images.  Hard errors (oversize, bad
mime, oversize count, extraction failure) raise ``DocumentExtractionError``
so the proxy can return an informative 4xx/5xx to the client.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import httpx

import config

log = logging.getLogger("pb-proxy.docs")


# ── Public types ─────────────────────────────────────────────


class DocumentExtractionError(Exception):
    """Raised when a document attachment cannot be processed.

    The proxy converts the status code and detail into an HTTP error
    response to the client.
    """

    def __init__(
        self, status_code: int, detail: str, mime_type: str | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.mime_type = mime_type or "unknown"


@dataclass
class DocumentExtractionResult:
    """Aggregate metadata for the telemetry block."""

    files: int = 0
    bytes_in_total: int = 0
    chars_out_total: int = 0
    extractors: list[str] = field(default_factory=list)
    mime_types: list[str] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)


# ── Shape helpers ────────────────────────────────────────────

# Parses data: URLs of the form `data:<mime>;base64,<payload>`.
_DATA_URL_RE = re.compile(r"^data:([^;,]+)(?:;base64)?,(.*)$", re.DOTALL)


def _extract_data_url(file_data: str) -> tuple[str, bytes] | None:
    """Decode a data URL and return (mime_type, bytes).

    Returns None if the payload is not valid base64 or not a data URL.
    """
    match = _DATA_URL_RE.match(file_data)
    if not match:
        return None
    mime_type = match.group(1).strip() or "application/octet-stream"
    b64 = match.group(2).strip()
    try:
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        return None
    return mime_type, raw


def _is_file_block(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    return part.get("type") in ("file", "input_file")


def _file_payload(part: dict) -> dict | None:
    """Return the inner file-descriptor dict regardless of OpenAI variant."""
    if part.get("type") == "file":
        return part.get("file") or None
    if part.get("type") == "input_file":
        return part.get("input_file") or None
    return None


# ── Main middleware ──────────────────────────────────────────


async def extract_documents_in_messages(
    messages: list[dict[str, Any]],
    http_client: httpx.AsyncClient,
    policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], DocumentExtractionResult]:
    """Extract every file/document block in ``messages`` to plain text.

    Args:
        messages: Chat messages (may contain multimodal content arrays).
        http_client: Shared httpx client — reuses connection pool.
        policy: OPA policy result dict with at least the keys
            ``documents_allowed`` (bool),
            ``documents_max_bytes`` (int),
            ``documents_allowed_mime_types`` (list[str] or set[str]),
            ``documents_max_files`` (int).

    Returns:
        (rewritten_messages, extraction_result). The returned list is a deep
        copy of the input with file blocks replaced by text blocks.

    Raises:
        DocumentExtractionError: extraction failed and the client must be
            informed (413 oversize, 415 mime, 422 empty, 502 ingestion down,
            504 timeout).
    """
    allowed = bool(policy.get("documents_allowed", False))
    max_bytes = int(policy.get("documents_max_bytes", 0) or 0)
    max_files = int(policy.get("documents_max_files", 0) or 0)
    allowed_mime = _as_set(policy.get("documents_allowed_mime_types", ()))

    result_msgs = deepcopy(messages)
    agg = DocumentExtractionResult()

    # Phase 1: enumerate all file blocks (for the per-request count check).
    file_locations: list[tuple[int, int, dict]] = []  # (msg_idx, part_idx, payload)
    for m_idx, msg in enumerate(result_msgs):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for p_idx, part in enumerate(content):
            if _is_file_block(part):
                payload = _file_payload(part)
                if payload is not None:
                    file_locations.append((m_idx, p_idx, payload))

    if not file_locations:
        return result_msgs, agg  # nothing to do

    if not allowed:
        # Policy denies attachments → remove them silently (same UX as images).
        for m_idx, p_idx, _ in file_locations:
            result_msgs[m_idx]["content"][p_idx] = {
                "type": "text",
                "text": "[Document attachment removed — not permitted for this role]",
            }
        return result_msgs, agg

    if max_files and len(file_locations) > max_files:
        raise DocumentExtractionError(
            status_code=413,
            detail=(
                f"Too many document attachments: {len(file_locations)} > {max_files}"
            ),
        )

    # Phase 2: process each file in the order they appear.
    for m_idx, p_idx, payload in file_locations:
        file_data = payload.get("file_data", "")
        filename = payload.get("filename", "attachment")

        parsed = _extract_data_url(file_data) if isinstance(file_data, str) else None
        if parsed is None:
            raise DocumentExtractionError(
                status_code=400,
                detail=f"File block for '{filename}' has invalid data URL",
            )
        mime_type, raw = parsed

        if max_bytes and len(raw) > max_bytes:
            raise DocumentExtractionError(
                status_code=413,
                detail=(
                    f"Document '{filename}' exceeds policy size cap "
                    f"({len(raw)} > {max_bytes} bytes)"
                ),
                mime_type=mime_type,
            )

        if allowed_mime and mime_type.lower() not in allowed_mime:
            raise DocumentExtractionError(
                status_code=415,
                detail=f"MIME type '{mime_type}' not allowed for document attachments",
                mime_type=mime_type,
            )

        # Send to ingestion service for extraction.
        b64_payload = base64.b64encode(raw).decode("ascii")
        try:
            resp = await http_client.post(
                f"{config.INGESTION_URL}/extract",
                json={
                    "data": b64_payload,
                    "filename": filename,
                    "mime_type": mime_type,
                    "max_bytes": max_bytes or None,
                },
                headers=config.ingestion_headers(),
                timeout=60.0,
            )
        except httpx.TimeoutException as exc:
            raise DocumentExtractionError(
                status_code=504,
                detail=f"Extraction timed out for '{filename}': {exc}",
                mime_type=mime_type,
            )
        except httpx.HTTPError as exc:
            raise DocumentExtractionError(
                status_code=502,
                detail=f"Extraction service unreachable for '{filename}': {exc}",
                mime_type=mime_type,
            )

        if resp.status_code >= 400:
            # Propagate the ingestion service's verdict (415/422/413/504/500).
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise DocumentExtractionError(
                status_code=resp.status_code,
                detail=f"Extraction failed for '{filename}': {detail}",
                mime_type=mime_type,
            )

        body = resp.json()
        text = body.get("text") or ""
        extractor = body.get("extractor", "unknown")

        if not text.strip():
            raise DocumentExtractionError(
                status_code=422,
                detail=f"Document '{filename}' produced no extractable text",
                mime_type=mime_type,
            )

        # Replace the block with a text part.
        result_msgs[m_idx]["content"][p_idx] = {
            "type": "text",
            "text": f"<File: {filename}>\n\n{text}",
        }

        agg.files += 1
        agg.bytes_in_total += len(raw)
        agg.chars_out_total += len(text)
        agg.extractors.append(extractor)
        agg.mime_types.append(mime_type)
        agg.filenames.append(filename)

    return result_msgs, agg


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, set):
        return {str(v).lower() for v in value}
    if isinstance(value, (list, tuple)):
        return {str(v).lower() for v in value}
    return set()
