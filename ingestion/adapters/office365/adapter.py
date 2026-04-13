"""Office 365 Adapter — SourceAdapter implementation for Microsoft Graph.

Orchestrates SharePoint, OneDrive, OneNote, Outlook, and Teams providers.
Implements the same SourceAdapter interface as GitAdapter for uniform sync.

Key difference from GitAdapter: uses delta_link (opaque token) instead of
commit SHA for incremental sync state tracking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ingestion.adapters.base import FileChange, NormalizedDocument, SourceAdapter
from ingestion.adapters.office365.content import ContentExtractor
from ingestion.adapters.office365.graph_client import GraphClient, GraphClientConfig
from ingestion.adapters.office365.providers.onenote import OneNoteConfig, OneNoteProvider
from ingestion.adapters.office365.providers.outlook import MailboxConfig, OutlookProvider
from ingestion.adapters.office365.providers.sharepoint import (
    SharePointProvider,
    SiteConfig,
)
from ingestion.adapters.office365.providers.teams import TeamConfig, TeamsProvider

log = logging.getLogger("pb-o365-adapter")


@dataclass
class Office365Config:
    """Configuration for an Office 365 source to sync."""

    name: str
    tenant_id: str
    client_id: str
    client_secret: str  # resolved from Docker Secret by loader

    # Target Qdrant collection and project
    collection: str = "pb_general"
    project: str = ""
    poll_interval_minutes: int = 15
    max_file_size_mb: int = 50

    # Data sources (all optional — configure what you need)
    sites: list[dict] = field(default_factory=list)
    onenote: list[dict] = field(default_factory=list)
    mailboxes: list[dict] = field(default_factory=list)
    teams: list[dict] = field(default_factory=list)

    # For OneNote delegated auth
    refresh_token: str | None = None

    # Resource unit budget per minute (depends on tenant license count)
    ru_budget_per_minute: int = 1250

    @property
    def site_configs(self) -> list[SiteConfig]:
        return [SiteConfig(**s) for s in self.sites]

    @property
    def onenote_configs(self) -> list[OneNoteConfig]:
        return [OneNoteConfig(**o) for o in self.onenote]

    @property
    def mailbox_configs(self) -> list[MailboxConfig]:
        return [MailboxConfig(**m) for m in self.mailboxes]

    @property
    def team_configs(self) -> list[TeamConfig]:
        return [TeamConfig(**t) for t in self.teams]


class Office365Adapter(SourceAdapter):
    """Adapter for Microsoft 365 content via Graph API.

    Unlike the Git adapter which uses commit SHAs, this adapter uses
    opaque delta links for incremental sync. The `get_current_sha()`
    method returns a timestamp token, and delta links are managed
    per-drive/folder/channel internally.
    """

    def __init__(self, config: Office365Config, client: httpx.AsyncClient):
        self.config = config
        self._extractor = ContentExtractor()

        graph_config = GraphClientConfig(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
            refresh_token=config.refresh_token,
            ru_budget_per_minute=config.ru_budget_per_minute,
        )
        self._graph = GraphClient(graph_config, client)
        self._sharepoint = SharePointProvider(
            self._graph, self._extractor,
            max_file_size=config.max_file_size_mb * 1024 * 1024,
        )
        self._outlook = OutlookProvider(self._graph, self._extractor)
        self._teams = TeamsProvider(self._graph, self._extractor)
        self._onenote = OneNoteProvider(self._graph, self._extractor)

        # Internal delta state — populated during sync
        self._delta_links: dict[str, str] = {}

    @property
    def delta_links(self) -> dict[str, str]:
        """Accumulated delta links from the last sync operation.

        Keys are resource identifiers like:
        - "drive:{drive_id}" for SharePoint/OneDrive
        - "mail:{user}:{folder_id}" for Outlook
        - "teams:{team_id}:{channel_id}" for Teams
        """
        return self._delta_links

    @delta_links.setter
    def delta_links(self, links: dict[str, str]) -> None:
        self._delta_links = links

    # ── SourceAdapter Interface ─────────────────────────────────

    async def get_current_sha(self) -> str:
        """Return a timestamp token (Office 365 has no single SHA equivalent)."""
        return datetime.now(timezone.utc).isoformat()

    async def fetch_all_files(self) -> list[NormalizedDocument]:
        """Initial sync: fetch all content from all configured sources."""
        docs: list[NormalizedDocument] = []
        self._delta_links = {}

        # SharePoint / OneDrive
        for site_cfg in self.config.site_configs:
            site_docs = await self._fetch_sharepoint_initial(site_cfg)
            docs.extend(site_docs)

        # Outlook Mail
        for mb_cfg in self.config.mailbox_configs:
            mail_docs = await self._fetch_outlook_initial(mb_cfg)
            docs.extend(mail_docs)

        # Teams
        for team_cfg in self.config.team_configs:
            team_docs = await self._fetch_teams_initial(team_cfg)
            docs.extend(team_docs)

        # OneNote
        for on_cfg in self.config.onenote_configs:
            on_docs = await self._fetch_onenote_initial(on_cfg)
            docs.extend(on_docs)

        log.info(
            "Initial sync for %s: %d documents (%d delta links)",
            self.config.name, len(docs), len(self._delta_links),
        )
        return docs

    async def fetch_changed_files(self, since_sha: str) -> list[NormalizedDocument]:
        """Incremental sync using stored delta links."""
        docs: list[NormalizedDocument] = []

        # SharePoint / OneDrive
        for site_cfg in self.config.site_configs:
            site_docs = await self._fetch_sharepoint_incremental(site_cfg)
            docs.extend(site_docs)

        # Outlook Mail
        for mb_cfg in self.config.mailbox_configs:
            mail_docs = await self._fetch_outlook_incremental(mb_cfg)
            docs.extend(mail_docs)

        # Teams
        for team_cfg in self.config.team_configs:
            team_docs = await self._fetch_teams_incremental(team_cfg)
            docs.extend(team_docs)

        # OneNote (uses timestamp, not delta link)
        for on_cfg in self.config.onenote_configs:
            on_docs = await self._fetch_onenote_incremental(on_cfg, since_sha)
            docs.extend(on_docs)

        log.info(
            "Incremental sync for %s: %d documents", self.config.name, len(docs),
        )
        return docs

    async def get_file_changes(self, since_sha: str) -> list[FileChange]:
        """Get all file changes for deletion tracking."""
        changes: list[FileChange] = []

        # SharePoint / OneDrive
        for site_cfg in self.config.site_configs:
            site_changes = await self._get_sharepoint_changes(site_cfg)
            changes.extend(site_changes)

        # Outlook Mail
        for mb_cfg in self.config.mailbox_configs:
            mail_changes = await self._get_outlook_changes(mb_cfg)
            changes.extend(mail_changes)

        # Teams
        for team_cfg in self.config.team_configs:
            team_changes = await self._get_teams_changes(team_cfg)
            changes.extend(team_changes)

        return changes

    # ── SharePoint Sync ─────────────────────────────────────────

    async def _fetch_sharepoint_initial(
        self, config: SiteConfig
    ) -> list[NormalizedDocument]:
        """Initial sync for a SharePoint site."""
        docs: list[NormalizedDocument] = []
        try:
            site_id = await self._sharepoint.resolve_site_id(config.url)
            drives = await self._sharepoint.get_drives(site_id)

            for drive in drives:
                drive_id = drive["id"]
                items, delta_link = await self._sharepoint.delta_sync(drive_id)
                self._delta_links[f"drive:{drive_id}"] = delta_link

                drive_docs = await self._sharepoint.fetch_documents(
                    drive_id, items, config, self.config.name,
                )
                docs.extend(drive_docs)
                log.info(
                    "SharePoint %s drive %s: %d items → %d docs",
                    config.url, drive.get("name", ""), len(items), len(drive_docs),
                )
        except Exception:
            log.exception("Failed to sync SharePoint site %s", config.url)

        return docs

    async def _fetch_sharepoint_incremental(
        self, config: SiteConfig
    ) -> list[NormalizedDocument]:
        """Incremental sync for a SharePoint site using delta links."""
        docs: list[NormalizedDocument] = []
        try:
            site_id = await self._sharepoint.resolve_site_id(config.url)
            drives = await self._sharepoint.get_drives(site_id)

            for drive in drives:
                drive_id = drive["id"]
                dl_key = f"drive:{drive_id}"
                delta_link = self._delta_links.get(dl_key)

                items, new_delta = await self._sharepoint.delta_sync(
                    drive_id, delta_link=delta_link,
                )
                self._delta_links[dl_key] = new_delta

                # Fetch only non-deleted items
                active_items = [i for i in items if "deleted" not in i]
                if active_items:
                    drive_docs = await self._sharepoint.fetch_documents(
                        drive_id, active_items, config, self.config.name,
                    )
                    docs.extend(drive_docs)
        except Exception:
            log.exception("Failed incremental sync for SharePoint %s", config.url)

        return docs

    async def _get_sharepoint_changes(
        self, config: SiteConfig
    ) -> list[FileChange]:
        """Get file changes from SharePoint delta for deletion tracking."""
        changes: list[FileChange] = []
        try:
            site_id = await self._sharepoint.resolve_site_id(config.url)
            drives = await self._sharepoint.get_drives(site_id)

            for drive in drives:
                drive_id = drive["id"]
                dl_key = f"drive:{drive_id}"
                delta_link = self._delta_links.get(dl_key)

                items, new_delta = await self._sharepoint.delta_sync(
                    drive_id, delta_link=delta_link,
                )
                self._delta_links[dl_key] = new_delta
                changes.extend(self._sharepoint.extract_changes(items, config))
        except Exception:
            log.exception("Failed to get SharePoint changes for %s", config.url)

        return changes

    # ── Outlook Sync ────────────────────────────────────────────

    async def _fetch_outlook_initial(
        self, config: MailboxConfig
    ) -> list[NormalizedDocument]:
        """Initial sync for a mailbox."""
        docs: list[NormalizedDocument] = []
        for folder_name in config.folders:
            try:
                folder_id = await self._outlook.resolve_folder_id(
                    config.user, folder_name
                )
                if not folder_id:
                    continue

                messages, delta_link = await self._outlook.delta_sync(
                    config.user, folder_id,
                )
                self._delta_links[f"mail:{config.user}:{folder_id}"] = delta_link

                folder_docs = await self._outlook.fetch_documents(
                    config.user, messages, config, self.config.name,
                )
                docs.extend(folder_docs)
                log.info(
                    "Outlook %s/%s: %d messages → %d docs",
                    config.user, folder_name, len(messages), len(folder_docs),
                )
            except Exception:
                log.exception(
                    "Failed to sync Outlook %s/%s", config.user, folder_name
                )
        return docs

    async def _fetch_outlook_incremental(
        self, config: MailboxConfig
    ) -> list[NormalizedDocument]:
        """Incremental sync for a mailbox."""
        docs: list[NormalizedDocument] = []
        for folder_name in config.folders:
            try:
                folder_id = await self._outlook.resolve_folder_id(
                    config.user, folder_name
                )
                if not folder_id:
                    continue

                dl_key = f"mail:{config.user}:{folder_id}"
                delta_link = self._delta_links.get(dl_key)

                messages, new_delta = await self._outlook.delta_sync(
                    config.user, folder_id, delta_link=delta_link,
                )
                self._delta_links[dl_key] = new_delta

                active = [m for m in messages if "@removed" not in m]
                if active:
                    folder_docs = await self._outlook.fetch_documents(
                        config.user, active, config, self.config.name,
                    )
                    docs.extend(folder_docs)
            except Exception:
                log.exception(
                    "Failed incremental sync for Outlook %s/%s",
                    config.user, folder_name,
                )
        return docs

    async def _get_outlook_changes(
        self, config: MailboxConfig
    ) -> list[FileChange]:
        """Get mail changes for deletion tracking."""
        changes: list[FileChange] = []
        for folder_name in config.folders:
            try:
                folder_id = await self._outlook.resolve_folder_id(
                    config.user, folder_name
                )
                if not folder_id:
                    continue

                dl_key = f"mail:{config.user}:{folder_id}"
                delta_link = self._delta_links.get(dl_key)

                messages, new_delta = await self._outlook.delta_sync(
                    config.user, folder_id, delta_link=delta_link,
                )
                self._delta_links[dl_key] = new_delta
                changes.extend(self._outlook.extract_changes(messages, config.user))
            except Exception:
                log.exception(
                    "Failed to get Outlook changes for %s/%s",
                    config.user, folder_name,
                )
        return changes

    # ── Teams Sync ──────────────────────────────────────────────

    async def _fetch_teams_initial(
        self, config: TeamConfig
    ) -> list[NormalizedDocument]:
        """Initial sync for a team's channels."""
        docs: list[NormalizedDocument] = []
        try:
            team_id = await self._teams.find_team_id(config.name)
            if not team_id:
                return docs

            channels = await self._teams.resolve_channels(team_id, config.channels)
            for channel in channels:
                channel_id = channel["id"]
                channel_name = channel.get("displayName", "")

                messages, delta_link = await self._teams.delta_sync(
                    team_id, channel_id,
                )
                self._delta_links[f"teams:{team_id}:{channel_id}"] = delta_link

                ch_docs = await self._teams.fetch_documents(
                    messages, config, channel_name, self.config.name,
                )
                docs.extend(ch_docs)
                log.info(
                    "Teams %s/%s: %d messages → %d docs",
                    config.name, channel_name, len(messages), len(ch_docs),
                )
        except Exception:
            log.exception("Failed to sync Teams %s", config.name)

        return docs

    async def _fetch_teams_incremental(
        self, config: TeamConfig
    ) -> list[NormalizedDocument]:
        """Incremental sync for a team's channels."""
        docs: list[NormalizedDocument] = []
        try:
            team_id = await self._teams.find_team_id(config.name)
            if not team_id:
                return docs

            channels = await self._teams.resolve_channels(team_id, config.channels)
            for channel in channels:
                channel_id = channel["id"]
                channel_name = channel.get("displayName", "")
                dl_key = f"teams:{team_id}:{channel_id}"
                delta_link = self._delta_links.get(dl_key)

                messages, new_delta = await self._teams.delta_sync(
                    team_id, channel_id, delta_link=delta_link,
                )
                self._delta_links[dl_key] = new_delta

                active = [m for m in messages if "@removed" not in m and not m.get("deletedDateTime")]
                if active:
                    ch_docs = await self._teams.fetch_documents(
                        active, config, channel_name, self.config.name,
                    )
                    docs.extend(ch_docs)
        except Exception:
            log.exception("Failed incremental sync for Teams %s", config.name)

        return docs

    async def _get_teams_changes(
        self, config: TeamConfig
    ) -> list[FileChange]:
        """Get Teams message changes for deletion tracking."""
        changes: list[FileChange] = []
        try:
            team_id = await self._teams.find_team_id(config.name)
            if not team_id:
                return changes

            channels = await self._teams.resolve_channels(team_id, config.channels)
            for channel in channels:
                channel_id = channel["id"]
                channel_name = channel.get("displayName", "")
                dl_key = f"teams:{team_id}:{channel_id}"
                delta_link = self._delta_links.get(dl_key)

                messages, new_delta = await self._teams.delta_sync(
                    team_id, channel_id, delta_link=delta_link,
                )
                self._delta_links[dl_key] = new_delta
                changes.extend(
                    self._teams.extract_changes(messages, config.name, channel_name)
                )
        except Exception:
            log.exception("Failed to get Teams changes for %s", config.name)

        return changes

    # ── OneNote Sync ────────────────────────────────────────────

    async def _fetch_onenote_initial(
        self, config: OneNoteConfig
    ) -> list[NormalizedDocument]:
        """Initial sync for a OneNote notebook."""
        try:
            site_id = None
            if config.site:
                site_id = await self._sharepoint.resolve_site_id(config.site)

            docs = await self._onenote.fetch_all_pages(
                config, self.config.name, site_id=site_id,
            )
            # Store sync timestamp (no delta link for OneNote)
            self._delta_links[f"onenote:{config.notebook}"] = (
                self._onenote.get_sync_token()
            )
            log.info("OneNote %s: %d pages", config.notebook, len(docs))
            return docs
        except Exception:
            log.exception("Failed to sync OneNote %s", config.notebook)
            return []

    async def _fetch_onenote_incremental(
        self, config: OneNoteConfig, since_sha: str
    ) -> list[NormalizedDocument]:
        """Incremental sync for OneNote (timestamp-based, no delta)."""
        try:
            site_id = None
            if config.site:
                site_id = await self._sharepoint.resolve_site_id(config.site)

            # Use stored timestamp or fallback to since_sha
            last_synced = self._delta_links.get(
                f"onenote:{config.notebook}", since_sha
            )

            docs = await self._onenote.fetch_changed_pages(
                config, self.config.name, last_synced, site_id=site_id,
            )
            self._delta_links[f"onenote:{config.notebook}"] = (
                self._onenote.get_sync_token()
            )
            return docs
        except Exception:
            log.exception("Failed incremental sync for OneNote %s", config.notebook)
            return []
