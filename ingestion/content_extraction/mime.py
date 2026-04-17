"""Canonical MIME-type ↔ extension mapping for document extraction.

Shared between:
- ingestion_api.py `/extract` endpoint (payload validation)
- OPA policy check in pb-proxy (document_allowed_mime_types)
- pb-proxy document_extraction.py (multimodal block parsing)
"""

from __future__ import annotations

# Canonical MIME types that the ContentExtractor can handle.
# Keep this in sync with opa-policies/pb/data.json `proxy.documents.allowed_mime_types`.
MIME_TO_EXTENSION: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-outlook": ".msg",
    "message/rfc822": ".eml",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/x-yaml": ".yaml",
    "text/yaml": ".yaml",
}

EXTENSION_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".msg": "application/vnd.ms-outlook",
    ".eml": "message/rfc822",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
}


def mime_type_to_extension(mime_type: str | None) -> str | None:
    """Return the canonical file extension for a MIME type, or None if unknown.

    The returned extension always starts with a dot (e.g. ".pdf").
    """
    if not mime_type:
        return None
    return MIME_TO_EXTENSION.get(mime_type.strip().lower())


def extension_to_mime_type(extension: str | None) -> str | None:
    """Return a canonical MIME type for a file extension, or None if unknown."""
    if not extension:
        return None
    ext = extension.strip().lower()
    if not ext.startswith("."):
        ext = "." + ext
    return EXTENSION_TO_MIME.get(ext)
