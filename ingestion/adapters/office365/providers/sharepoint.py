"""SharePoint / OneDrive provider — syncs document libraries via Microsoft Graph.

Uses Delta Queries for incremental sync. Supports include/exclude glob patterns.
Uses cTag property to distinguish content changes from metadata-only changes.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass

from ingestion.adapters.base import FileChange, NormalizedDocument
from ingestion.adapters.office365.content import (
    ContentExtractor,
    can_extract,
    detect_content_type,
    should_skip_file,
)
from ingestion.adapters.office365.graph_client import GraphClient, RU_COSTS

log = logging.getLogger("pb-o365-sharepoint")

# Default directories to skip
SKIP_DIRS = frozenset({
    "_catalogs", "_cts", "forms", "_private",
    "node_modules", ".git", "__pycache__", "vendor",
})

# Default max file size (bytes)
DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def _matches_glob(path: str, patterns: list[str]) -> bool:
    """Match path against glob patterns, supporting ** for recursive dirs.

    - "Docs/**/*.docx" matches "Docs/report.docx" and "Docs/sub/report.docx"
    - "**/*.py" matches "src/main.py" and "main.py"
    """
    import re

    for pattern in patterns:
        if "**" in pattern:
            # Convert glob to regex piece by piece
            parts = re.split(r"(\*\*/|\*\*)", pattern)
            regex_parts: list[str] = []
            for part in parts:
                if part == "**/":
                    regex_parts.append("(.+/)?")
                elif part == "**":
                    regex_parts.append(".*")
                else:
                    # Escape dots, convert single * and ?
                    p = re.escape(part)
                    p = p.replace(r"\*", "[^/]*")
                    p = p.replace(r"\?", "[^/]")
                    regex_parts.append(p)
            regex = "".join(regex_parts)
            if re.fullmatch(regex, path):
                return True
        else:
            if fnmatch.fnmatch(path, pattern):
                return True
    return False


@dataclass
class SiteConfig:
    """Configuration for a single SharePoint site to sync."""

    url: str
    classification: str = "internal"
    include: list[str] | None = None
    exclude: list[str] | None = None


class SharePointProvider:
    """Fetch documents from SharePoint sites and OneDrive via Graph API."""

    def __init__(
        self,
        client: GraphClient,
        extractor: ContentExtractor,
        *,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    ):
        self.client = client
        self.extractor = extractor
        self.max_file_size = max_file_size

    # ── Site Resolution ─────────────────────────────────────────

    async def resolve_site_id(self, site_url: str) -> str:
        """Resolve a SharePoint site URL to a Graph site ID.

        Accepts: https://tenant.sharepoint.com/sites/sitename
        """
        # Extract hostname and site path from URL
        from urllib.parse import urlparse
        parsed = urlparse(site_url)
        hostname = parsed.hostname
        # /sites/sitename → sites/sitename
        site_path = parsed.path.strip("/")

        data = await self.client.get(
            f"/sites/{hostname}:/{site_path}",
            ru_cost=RU_COSTS["get_item"],
        )
        site_id = data["id"]
        log.debug("Resolved %s → site_id=%s", site_url, site_id)
        return site_id

    async def get_drives(self, site_id: str) -> list[dict]:
        """List all document libraries (drives) for a site."""
        return await self.client.get_all_pages(
            f"/sites/{site_id}/drives",
            ru_cost=RU_COSTS["list"],
        )

    # ── File Listing & Delta ────────────────────────────────────

    async def delta_sync(
        self,
        drive_id: str,
        delta_link: str | None = None,
        select: str = "id,name,size,file,folder,deleted,parentReference,lastModifiedDateTime,cTag",
    ) -> tuple[list[dict], str]:
        """Delta query on a drive. Returns (changed_items, new_delta_link)."""
        params = {"$select": select} if not delta_link else None
        return await self.client.delta_query(
            f"/drives/{drive_id}/root/delta",
            delta_link=delta_link,
            params=params,
        )

    async def download_file(self, drive_id: str, item_id: str) -> bytes:
        """Download file content by drive and item ID."""
        return await self.client.get_binary(
            f"/drives/{drive_id}/items/{item_id}/content",
            ru_cost=RU_COSTS["download"],
        )

    # ── Filtering ───────────────────────────────────────────────

    def _item_path(self, item: dict) -> str:
        """Extract relative file path from a drive item."""
        parent = item.get("parentReference", {})
        parent_path = parent.get("path", "")
        # path looks like: /drives/{id}/root:/folder/subfolder
        if ":/" in parent_path:
            parent_path = parent_path.split(":/", 1)[1]
        else:
            parent_path = ""
        name = item.get("name", "")
        return f"{parent_path}/{name}".lstrip("/") if parent_path else name

    def _should_include(
        self, path: str, config: SiteConfig
    ) -> bool:
        """Check if a file path matches include/exclude patterns."""
        # Skip known directories
        parts = path.split("/")
        for part in parts[:-1]:
            if part.lower() in SKIP_DIRS:
                return False

        # Skip binary/unsupported files
        if should_skip_file(path):
            return False

        if not can_extract(path):
            return False

        # Apply exclude patterns (supports ** for recursive matching)
        if config.exclude:
            if _matches_glob(path, config.exclude):
                return False

        # Apply include patterns (if set, must match at least one)
        if config.include:
            if not _matches_glob(path, config.include):
                return False

        return True

    def _is_deleted(self, item: dict) -> bool:
        """Check if an item was deleted (delta query marker)."""
        return "deleted" in item

    def _is_file(self, item: dict) -> bool:
        """Check if an item is a file (not a folder)."""
        return "file" in item

    # ── Document Conversion ─────────────────────────────────────

    async def fetch_documents(
        self,
        drive_id: str,
        items: list[dict],
        config: SiteConfig,
        source_name: str,
    ) -> list[NormalizedDocument]:
        """Download and convert a list of drive items to NormalizedDocuments."""
        docs: list[NormalizedDocument] = []

        for item in items:
            if not self._is_file(item):
                continue
            if self._is_deleted(item):
                continue

            path = self._item_path(item)
            if not self._should_include(path, config):
                continue

            size = item.get("size", 0)
            if size > self.max_file_size:
                log.info("Skipping oversized file (%d MB): %s", size // (1024 * 1024), path)
                continue

            try:
                data = await self.download_file(drive_id, item["id"])
                text = self.extractor.extract_from_bytes(data, item["name"])
                if not text:
                    continue

                doc = NormalizedDocument(
                    content=text,
                    content_type=detect_content_type(item["name"]),
                    source_ref=f"office365:{source_name}:{path}@{item.get('cTag', item['id'])}",
                    source_type="office365",
                    metadata={
                        "site_url": config.url,
                        "file_path": path,
                        "drive_id": drive_id,
                        "item_id": item["id"],
                        "last_modified": item.get("lastModifiedDateTime", ""),
                        "size": size,
                    },
                )
                docs.append(doc)
            except Exception:
                log.warning("Failed to fetch/extract %s", path, exc_info=True)

        return docs

    def extract_changes(
        self,
        items: list[dict],
        config: SiteConfig,
    ) -> list[FileChange]:
        """Convert delta items to FileChange objects for deletion tracking."""
        changes: list[FileChange] = []

        for item in items:
            path = self._item_path(item)

            if self._is_deleted(item):
                changes.append(FileChange(path=path, status="removed"))
            elif self._is_file(item):
                if not self._should_include(path, config):
                    continue
                # Delta doesn't distinguish add vs modify — treat all as modified
                changes.append(FileChange(path=path, status="modified"))

        return changes
