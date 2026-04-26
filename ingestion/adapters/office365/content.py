"""Backward-compat shim.

Content extraction moved to :mod:`ingestion.content_extraction` so it can be
reused by other adapters and by the `/extract` endpoint consumed by pb-proxy.

All existing imports continue to work unchanged:

    from ingestion.adapters.office365.content import ContentExtractor, detect_content_type
"""

from __future__ import annotations

from ingestion.content_extraction import (  # noqa: F401  (re-exports)
    BINARY_SKIP,
    CONTENT_TYPE_MAP,
    ContentExtractor,
    MARKITDOWN_EXTENSIONS,
    TEXT_EXTENSIONS,
    can_extract,
    detect_content_type,
    should_skip_file,
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
]
