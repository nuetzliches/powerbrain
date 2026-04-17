"""Shared content extraction for Powerbrain adapters and services.

Used by:
- Office 365 adapter (ingestion/adapters/office365/)
- GitHub adapter (when `allow_documents: true` in repos.yaml)
- POST /extract endpoint (ingestion_api.py) — called by pb-proxy for chat attachments

Primary backend: Microsoft markitdown (DOCX, PPTX, XLSX, PDF, MSG → Markdown).
Fallbacks: python-docx, openpyxl, python-pptx for specific formats.
Optional OCR fallback via Tesseract for scanned PDFs (OCR_FALLBACK_ENABLED).
"""

from __future__ import annotations

from .extractor import (
    ContentExtractor,
    detect_content_type,
    should_skip_file,
    can_extract,
    MARKITDOWN_EXTENSIONS,
    BINARY_SKIP,
    TEXT_EXTENSIONS,
    CONTENT_TYPE_MAP,
)
from .mime import (
    MIME_TO_EXTENSION,
    EXTENSION_TO_MIME,
    mime_type_to_extension,
    extension_to_mime_type,
)

__all__ = [
    "ContentExtractor",
    "detect_content_type",
    "should_skip_file",
    "can_extract",
    "MARKITDOWN_EXTENSIONS",
    "BINARY_SKIP",
    "TEXT_EXTENSIONS",
    "CONTENT_TYPE_MAP",
    "MIME_TO_EXTENSION",
    "EXTENSION_TO_MIME",
    "mime_type_to_extension",
    "extension_to_mime_type",
]
