"""Teams Channel provider — syncs channel messages via Microsoft Graph Delta Queries.

Threading: replies are collected into a single document per conversation thread.
Deduplication: file attachments reference SharePoint; only message text is indexed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ingestion.adapters.base import FileChange, NormalizedDocument
from ingestion.adapters.office365.content import ContentExtractor
from ingestion.adapters.office365.graph_client import GraphClient, RU_COSTS

log = logging.getLogger("pb-o365-teams")


@dataclass
class TeamConfig:
    """Configuration for a single team to sync."""

    name: str
    channels: list[str]  # channel names, or ["*"] for all
    classification: str = "internal"


class TeamsProvider:
    """Fetch channel messages from Microsoft Teams via Graph API."""

    def __init__(self, client: GraphClient, extractor: ContentExtractor):
        self.client = client
        self.extractor = extractor

    # ── Team & Channel Resolution ───────────────────────────────

    async def find_team_id(self, team_name: str) -> str | None:
        """Find a team ID by display name."""
        teams = await self.client.get_all_pages(
            "/teams",
            params={"$filter": f"displayName eq '{team_name}'", "$select": "id,displayName"},
            ru_cost=RU_COSTS["list"],
        )
        if teams:
            return teams[0]["id"]
        log.warning("Team '%s' not found", team_name)
        return None

    async def get_channels(self, team_id: str) -> list[dict]:
        """List all channels for a team."""
        return await self.client.get_all_pages(
            f"/teams/{team_id}/channels",
            params={"$select": "id,displayName,membershipType"},
            ru_cost=RU_COSTS["list"],
        )

    async def resolve_channels(
        self, team_id: str, channel_names: list[str]
    ) -> list[dict]:
        """Resolve channel names to channel objects. '*' means all channels."""
        all_channels = await self.get_channels(team_id)
        if "*" in channel_names:
            return all_channels

        name_set = {n.lower() for n in channel_names}
        matched = [c for c in all_channels if c.get("displayName", "").lower() in name_set]
        unmatched = name_set - {c["displayName"].lower() for c in matched}
        if unmatched:
            log.warning("Channels not found: %s", ", ".join(unmatched))
        return matched

    # ── Delta Sync ──────────────────────────────────────────────

    async def delta_sync(
        self,
        team_id: str,
        channel_id: str,
        delta_link: str | None = None,
    ) -> tuple[list[dict], str]:
        """Delta query for channel messages. Returns (messages, new_delta_link)."""
        params = {
            "$select": "id,body,from,createdDateTime,lastEditedDateTime,"
                       "deletedDateTime,replyToId,attachments,reactions",
            "$expand": "replies($select=id,body,from,createdDateTime)",
        } if not delta_link else None

        return await self.client.delta_query(
            f"/teams/{team_id}/channels/{channel_id}/messages/delta",
            delta_link=delta_link,
            params=params,
        )

    # ── Message Processing ──────────────────────────────────────

    async def fetch_documents(
        self,
        messages: list[dict],
        config: TeamConfig,
        channel_name: str,
        source_name: str,
    ) -> list[NormalizedDocument]:
        """Convert Teams channel messages to NormalizedDocuments.

        Groups root messages with their replies into single documents (threads).
        File attachments are NOT indexed (they live in SharePoint).
        """
        docs: list[NormalizedDocument] = []

        for msg in messages:
            if "@removed" in msg:
                continue
            if msg.get("deletedDateTime"):
                continue
            # Skip reply-only messages (they're fetched via $expand on root)
            if msg.get("replyToId"):
                continue

            try:
                doc = self._thread_to_document(msg, config, channel_name, source_name)
                if doc:
                    docs.append(doc)
            except Exception:
                log.warning(
                    "Failed to process message %s", msg.get("id", "?"), exc_info=True
                )

        return docs

    def _thread_to_document(
        self,
        msg: dict,
        config: TeamConfig,
        channel_name: str,
        source_name: str,
    ) -> NormalizedDocument | None:
        """Convert a root message + replies into a single NormalizedDocument."""
        parts: list[str] = []

        # Root message
        root_text = self._extract_message_text(msg)
        if root_text:
            sender = _get_sender(msg)
            ts = msg.get("createdDateTime", "")
            parts.append(f"[{ts}] {sender}: {root_text}")

        # Replies (from $expand)
        replies = msg.get("replies", [])
        for reply in replies:
            if reply.get("deletedDateTime"):
                continue
            reply_text = self._extract_message_text(reply)
            if reply_text:
                sender = _get_sender(reply)
                ts = reply.get("createdDateTime", "")
                parts.append(f"[{ts}] {sender}: {reply_text}")

        if not parts:
            return None

        content = "\n".join(parts)

        # Extract file attachment names (for metadata, not indexing)
        attachment_names = self._get_file_attachment_names(msg)

        return NormalizedDocument(
            content=content,
            content_type="text",
            source_ref=f"office365:{source_name}:teams/{config.name}/{channel_name}/{msg['id']}",
            source_type="teams",
            metadata={
                "team": config.name,
                "channel": channel_name,
                "message_id": msg["id"],
                "created_at": msg.get("createdDateTime", ""),
                "reply_count": len(replies),
                "sender": _get_sender(msg),
                "file_attachments": attachment_names,
            },
        )

    def _extract_message_text(self, msg: dict) -> str:
        """Extract text content from a Teams message body."""
        body = msg.get("body", {})
        content = body.get("content", "")
        content_type = body.get("contentType", "text")

        if not content.strip():
            return ""

        if content_type.lower() == "html":
            return self.extractor.extract_html_to_text(content)

        return content.strip()

    @staticmethod
    def _get_file_attachment_names(msg: dict) -> list[str]:
        """Extract names of file attachments (for metadata only).

        File attachments in Teams are SharePoint references — we store
        the names for context but do NOT re-index the content to avoid
        duplication with the SharePoint provider.
        """
        names: list[str] = []
        for att in msg.get("attachments", []):
            if att.get("contentType") == "reference":
                names.append(att.get("name", "unknown"))
        return names

    def extract_changes(
        self, messages: list[dict], team_name: str, channel_name: str
    ) -> list[FileChange]:
        """Convert delta messages to FileChange objects for deletion tracking."""
        changes: list[FileChange] = []
        for msg in messages:
            path = f"teams/{team_name}/{channel_name}/{msg.get('id', '')}"
            if "@removed" in msg or msg.get("deletedDateTime"):
                changes.append(FileChange(path=path, status="removed"))
            elif not msg.get("replyToId"):
                # Only track root messages (replies are part of parent doc)
                changes.append(FileChange(path=path, status="modified"))
        return changes


def _get_sender(msg: dict) -> str:
    """Extract sender display name from a message."""
    from_field = msg.get("from", {})
    user = from_field.get("user", {})
    return user.get("displayName", user.get("id", "unknown"))
