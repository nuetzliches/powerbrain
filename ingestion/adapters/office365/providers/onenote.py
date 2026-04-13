"""OneNote provider — syncs notebook pages via Microsoft Graph (delegated auth).

Critical limitations (as of 2025):
- App-only auth deprecated since March 31, 2025 → requires delegated auth
- No delta query support → polls via lastModifiedDateTime comparison
- No webhook support → only polling-based sync
- Content returned as HTML → converted to text via BeautifulSoup
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ingestion.adapters.base import FileChange, NormalizedDocument
from ingestion.adapters.office365.content import ContentExtractor
from ingestion.adapters.office365.graph_client import GraphClient, RU_COSTS

log = logging.getLogger("pb-o365-onenote")


@dataclass
class OneNoteConfig:
    """Configuration for a OneNote notebook to sync."""

    notebook: str  # notebook display name
    site: str | None = None  # optional site URL (for site-scoped notebooks)
    classification: str = "internal"


class OneNoteProvider:
    """Fetch OneNote pages via Microsoft Graph (delegated auth required)."""

    def __init__(self, client: GraphClient, extractor: ContentExtractor):
        self.client = client
        self.extractor = extractor

    # ── Notebook Resolution ─────────────────────────────────────

    async def find_notebook(
        self, notebook_name: str, site_id: str | None = None
    ) -> dict | None:
        """Find a notebook by display name, optionally scoped to a site."""
        if site_id:
            path = f"/sites/{site_id}/onenote/notebooks"
        else:
            path = "/me/onenote/notebooks"

        notebooks = await self.client.get_all_pages(
            path,
            params={"$select": "id,displayName,lastModifiedDateTime"},
            delegated=True,
            ru_cost=RU_COSTS["list"],
        )

        for nb in notebooks:
            if nb.get("displayName", "").lower() == notebook_name.lower():
                return nb

        log.warning("Notebook '%s' not found", notebook_name)
        return None

    async def get_sections(self, notebook_id: str) -> list[dict]:
        """List all sections in a notebook."""
        return await self.client.get_all_pages(
            f"/notebooks/{notebook_id}/sections",
            params={"$select": "id,displayName"},
            delegated=True,
            ru_cost=RU_COSTS["list"],
        )

    async def get_pages(
        self, section_id: str, modified_since: str | None = None
    ) -> list[dict]:
        """List pages in a section, optionally filtered by modification time."""
        params: dict = {
            "$select": "id,title,lastModifiedDateTime,createdDateTime",
            "$orderby": "lastModifiedDateTime desc",
        }
        if modified_since:
            params["$filter"] = f"lastModifiedDateTime gt {modified_since}"

        return await self.client.get_all_pages(
            f"/sections/{section_id}/pages",
            params=params,
            delegated=True,
            ru_cost=RU_COSTS["list"],
        )

    async def get_page_content(self, page_id: str) -> str:
        """Fetch page content as HTML."""
        resp = await self.client.request(
            "GET",
            f"/pages/{page_id}/content",
            delegated=True,
            ru_cost=RU_COSTS["get_item"],
        )
        return resp.text

    # ── Full & Incremental Sync ─────────────────────────────────

    async def fetch_all_pages(
        self,
        config: OneNoteConfig,
        source_name: str,
        site_id: str | None = None,
    ) -> list[NormalizedDocument]:
        """Fetch all pages from a notebook (initial sync)."""
        notebook = await self.find_notebook(config.notebook, site_id)
        if not notebook:
            return []

        return await self._fetch_pages_from_notebook(
            notebook, config, source_name, modified_since=None
        )

    async def fetch_changed_pages(
        self,
        config: OneNoteConfig,
        source_name: str,
        last_synced_at: str,
        site_id: str | None = None,
    ) -> list[NormalizedDocument]:
        """Fetch pages modified since last sync (incremental, no delta API)."""
        notebook = await self.find_notebook(config.notebook, site_id)
        if not notebook:
            return []

        return await self._fetch_pages_from_notebook(
            notebook, config, source_name, modified_since=last_synced_at
        )

    async def _fetch_pages_from_notebook(
        self,
        notebook: dict,
        config: OneNoteConfig,
        source_name: str,
        modified_since: str | None,
    ) -> list[NormalizedDocument]:
        """Iterate sections and pages, download content, convert to documents."""
        docs: list[NormalizedDocument] = []
        notebook_id = notebook["id"]
        notebook_name = notebook.get("displayName", config.notebook)

        sections = await self.get_sections(notebook_id)
        log.info(
            "OneNote %s: %d sections, modified_since=%s",
            notebook_name, len(sections), modified_since or "initial",
        )

        for section in sections:
            section_name = section.get("displayName", "")
            pages = await self.get_pages(section["id"], modified_since)

            for page in pages:
                try:
                    html = await self.get_page_content(page["id"])
                    text = self.extractor.extract_html_to_text(html)
                    if not text or not text.strip():
                        continue

                    title = page.get("title", "Untitled")
                    # Prepend title for context
                    content = f"# {title}\n\n{text}"

                    doc = NormalizedDocument(
                        content=content,
                        content_type="markdown",
                        source_ref=(
                            f"office365:{source_name}:onenote/{notebook_name}"
                            f"/{section_name}/{page['id']}"
                        ),
                        source_type="onenote",
                        metadata={
                            "notebook": notebook_name,
                            "section": section_name,
                            "page_title": title,
                            "page_id": page["id"],
                            "last_modified": page.get("lastModifiedDateTime", ""),
                            "created_at": page.get("createdDateTime", ""),
                        },
                    )
                    docs.append(doc)
                except Exception:
                    log.warning(
                        "Failed to fetch OneNote page %s/%s/%s",
                        notebook_name, section_name, page.get("title", "?"),
                        exc_info=True,
                    )

        return docs

    def get_sync_token(self) -> str:
        """OneNote has no delta — use current timestamp as sync token."""
        return datetime.now(timezone.utc).isoformat()

    def extract_changes_from_pages(
        self,
        current_pages: list[dict],
        previous_page_ids: set[str],
        notebook_name: str,
    ) -> list[FileChange]:
        """Detect removed pages by comparing current page IDs vs previous."""
        current_ids = {p["id"] for p in current_pages}
        changes: list[FileChange] = []

        # Removed pages
        for page_id in previous_page_ids - current_ids:
            changes.append(FileChange(
                path=f"onenote/{notebook_name}/{page_id}",
                status="removed",
            ))

        # Modified/new pages
        for page in current_pages:
            changes.append(FileChange(
                path=f"onenote/{notebook_name}/{page['id']}",
                status="modified",
            ))

        return changes
