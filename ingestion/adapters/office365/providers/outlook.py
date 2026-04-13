"""Outlook Mail provider — syncs email messages via Microsoft Graph Delta Queries.

Extracts message body (HTML → text) and processes attachments via ContentExtractor.
High PII density expected: sender, recipients, signatures are pseudonymized by pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ingestion.adapters.base import FileChange, NormalizedDocument
from ingestion.adapters.office365.content import ContentExtractor, detect_content_type
from ingestion.adapters.office365.graph_client import GraphClient, RU_COSTS

log = logging.getLogger("pb-o365-outlook")

# Max attachment size to process
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB

# Attachment content types to skip
SKIP_ATTACHMENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/bmp", "image/svg+xml",
    "application/zip", "application/x-rar-compressed", "application/x-7z-compressed",
    "video/mp4", "video/avi", "audio/mpeg", "audio/wav",
})


@dataclass
class MailboxConfig:
    """Configuration for a single mailbox to sync."""

    user: str  # UPN or email, e.g. "support@corp.com"
    folders: list[str]  # folder names, e.g. ["Inbox", "Sent Items"]
    classification: str = "internal"
    max_age_months: int = 12  # only sync mails from last N months


class OutlookProvider:
    """Fetch email messages from Outlook via Microsoft Graph."""

    def __init__(self, client: GraphClient, extractor: ContentExtractor):
        self.client = client
        self.extractor = extractor

    # ── Folder Resolution ───────────────────────────────────────

    async def resolve_folder_id(self, user: str, folder_name: str) -> str | None:
        """Resolve a well-known folder name to a folder ID."""
        # Try well-known names first
        well_known = {
            "inbox": "inbox",
            "sent items": "sentitems",
            "sent": "sentitems",
            "drafts": "drafts",
            "deleted items": "deleteditems",
            "archive": "archive",
            "junk email": "junkemail",
        }
        alias = well_known.get(folder_name.lower())
        if alias:
            try:
                data = await self.client.get(
                    f"/users/{user}/mailFolders/{alias}",
                    ru_cost=RU_COSTS["get_item"],
                )
                return data["id"]
            except Exception:
                pass

        # Search by display name
        folders = await self.client.get_all_pages(
            f"/users/{user}/mailFolders",
            ru_cost=RU_COSTS["list"],
        )
        for folder in folders:
            if folder.get("displayName", "").lower() == folder_name.lower():
                return folder["id"]

        log.warning("Folder '%s' not found for user %s", folder_name, user)
        return None

    # ── Delta Sync ──────────────────────────────────────────────

    async def delta_sync(
        self,
        user: str,
        folder_id: str,
        delta_link: str | None = None,
    ) -> tuple[list[dict], str]:
        """Delta query for messages in a folder. Returns (messages, new_delta_link)."""
        params = {
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                       "body,hasAttachments,isRead,conversationId",
        } if not delta_link else None

        return await self.client.delta_query(
            f"/users/{user}/mailFolders/{folder_id}/messages/delta",
            delta_link=delta_link,
            params=params,
        )

    # ── Message Processing ──────────────────────────────────────

    async def fetch_documents(
        self,
        user: str,
        messages: list[dict],
        config: MailboxConfig,
        source_name: str,
    ) -> list[NormalizedDocument]:
        """Convert email messages to NormalizedDocuments."""
        docs: list[NormalizedDocument] = []

        for msg in messages:
            if "@removed" in msg:
                continue

            try:
                doc = self._message_to_document(msg, user, config, source_name)
                if doc:
                    docs.append(doc)

                # Process attachments if present
                if msg.get("hasAttachments"):
                    att_docs = await self._process_attachments(
                        user, msg, config, source_name
                    )
                    docs.extend(att_docs)
            except Exception:
                log.warning(
                    "Failed to process message %s", msg.get("id", "?"), exc_info=True
                )

        return docs

    def _message_to_document(
        self,
        msg: dict,
        user: str,
        config: MailboxConfig,
        source_name: str,
    ) -> NormalizedDocument | None:
        """Convert a single email message to a NormalizedDocument."""
        body = msg.get("body", {})
        content_type = body.get("contentType", "text")
        content = body.get("content", "")

        if not content.strip():
            return None

        # Convert HTML body to text
        if content_type.lower() == "html":
            content = self.extractor.extract_html_to_text(content)

        if not content.strip():
            return None

        subject = msg.get("subject", "(no subject)")
        sender = _extract_email(msg.get("from", {}))
        recipients = [_extract_email(r) for r in msg.get("toRecipients", [])]
        cc = [_extract_email(r) for r in msg.get("ccRecipients", [])]
        received = msg.get("receivedDateTime", "")

        # Compose text: subject + body
        text = f"Subject: {subject}\nFrom: {sender}\nTo: {', '.join(recipients)}\n"
        if cc:
            text += f"CC: {', '.join(cc)}\n"
        text += f"Date: {received}\n\n{content}"

        return NormalizedDocument(
            content=text,
            content_type="text",
            source_ref=f"office365:{source_name}:mail/{user}/{msg['id']}",
            source_type="email",
            metadata={
                "mailbox": user,
                "subject": subject,
                "sender": sender,
                "recipients": recipients,
                "cc": cc,
                "received_at": received,
                "conversation_id": msg.get("conversationId", ""),
                "has_attachments": msg.get("hasAttachments", False),
            },
        )

    async def _process_attachments(
        self,
        user: str,
        msg: dict,
        config: MailboxConfig,
        source_name: str,
    ) -> list[NormalizedDocument]:
        """Download and extract text from email attachments."""
        docs: list[NormalizedDocument] = []

        try:
            attachments = await self.client.get_all_pages(
                f"/users/{user}/messages/{msg['id']}/attachments",
                ru_cost=RU_COSTS["get_item"],
            )
        except Exception:
            log.warning("Failed to fetch attachments for message %s", msg["id"])
            return docs

        for att in attachments:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue

            name = att.get("name", "attachment")
            size = att.get("size", 0)
            content_type = att.get("contentType", "")

            if size > MAX_ATTACHMENT_SIZE:
                log.debug("Skipping large attachment %s (%d MB)", name, size // (1024 * 1024))
                continue

            if content_type in SKIP_ATTACHMENT_TYPES:
                continue

            content_bytes = att.get("contentBytes")
            if not content_bytes:
                continue

            import base64
            data = base64.b64decode(content_bytes)
            text = self.extractor.extract_from_bytes(data, name)
            if not text:
                continue

            docs.append(NormalizedDocument(
                content=text,
                content_type=detect_content_type(name),
                source_ref=f"office365:{source_name}:mail/{user}/{msg['id']}/att/{att['id']}",
                source_type="email",
                metadata={
                    "mailbox": user,
                    "message_id": msg["id"],
                    "attachment_name": name,
                    "attachment_size": size,
                    "subject": msg.get("subject", ""),
                },
            ))

        return docs

    def extract_changes(self, messages: list[dict], user: str) -> list[FileChange]:
        """Convert delta messages to FileChange objects for deletion tracking."""
        changes: list[FileChange] = []
        for msg in messages:
            msg_path = f"mail/{user}/{msg.get('id', '')}"
            if "@removed" in msg:
                changes.append(FileChange(path=msg_path, status="removed"))
            else:
                changes.append(FileChange(path=msg_path, status="modified"))
        return changes


def _extract_email(recipient: dict) -> str:
    """Extract email address from a Graph API recipient object."""
    ea = recipient.get("emailAddress", {})
    return ea.get("address", ea.get("name", "unknown"))
