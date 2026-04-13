"""Tests for Office 365 providers (SharePoint, Outlook, Teams, OneNote)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ingestion.adapters.office365.content import ContentExtractor
from ingestion.adapters.office365.graph_client import GRAPH_BASE, GraphClient, GraphClientConfig
from ingestion.adapters.office365.providers.outlook import OutlookProvider, MailboxConfig, _extract_email
from ingestion.adapters.office365.providers.sharepoint import SharePointProvider, SiteConfig
from ingestion.adapters.office365.providers.teams import TeamsProvider, TeamConfig, _get_sender


# ── Fixtures ────────────────────────────────────────────────


def _mock_token():
    """Set up respx to mock token acquisition."""
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )


@pytest.fixture
def graph_client():
    config = GraphClientConfig(
        tenant_id="test-tenant",
        client_id="test-client",
        client_secret="test-secret",
    )
    return GraphClient(config, httpx.AsyncClient())


@pytest.fixture
def extractor():
    return ContentExtractor()


@pytest.fixture
def sp_provider(graph_client, extractor):
    return SharePointProvider(graph_client, extractor)


@pytest.fixture
def outlook_provider(graph_client, extractor):
    return OutlookProvider(graph_client, extractor)


@pytest.fixture
def teams_provider(graph_client, extractor):
    return TeamsProvider(graph_client, extractor)


# ── SharePoint Tests ────────────────────────────────────────


class TestSharePointFiltering:
    def test_should_include_docx(self, sp_provider):
        config = SiteConfig(url="https://t.sharepoint.com/sites/s")
        assert sp_provider._should_include("Docs/report.docx", config) is True

    def test_skip_binary(self, sp_provider):
        config = SiteConfig(url="https://t.sharepoint.com/sites/s")
        assert sp_provider._should_include("images/logo.png", config) is False

    def test_skip_node_modules(self, sp_provider):
        config = SiteConfig(url="https://t.sharepoint.com/sites/s")
        assert sp_provider._should_include("node_modules/pkg/index.js", config) is False

    def test_include_pattern_matches(self, sp_provider):
        config = SiteConfig(
            url="https://t.sharepoint.com/sites/s",
            include=["Docs/**/*.docx"],
        )
        assert sp_provider._should_include("Docs/report.docx", config) is True
        assert sp_provider._should_include("Other/report.docx", config) is False

    def test_exclude_pattern(self, sp_provider):
        config = SiteConfig(
            url="https://t.sharepoint.com/sites/s",
            exclude=["Drafts/**"],
        )
        assert sp_provider._should_include("Drafts/draft.docx", config) is False
        assert sp_provider._should_include("Final/report.docx", config) is True

    def test_is_deleted(self, sp_provider):
        assert sp_provider._is_deleted({"deleted": {"state": "deleted"}}) is True
        assert sp_provider._is_deleted({"id": "123"}) is False

    def test_is_file(self, sp_provider):
        assert sp_provider._is_file({"file": {"mimeType": "text/plain"}}) is True
        assert sp_provider._is_file({"folder": {"childCount": 5}}) is False

    def test_item_path(self, sp_provider):
        item = {
            "name": "report.docx",
            "parentReference": {"path": "/drives/d1/root:/Documents/Legal"},
        }
        assert sp_provider._item_path(item) == "Documents/Legal/report.docx"

    def test_item_path_root(self, sp_provider):
        item = {
            "name": "readme.md",
            "parentReference": {"path": "/drives/d1/root:"},
        }
        assert sp_provider._item_path(item) == "readme.md"


@respx.mock
@pytest.mark.asyncio
async def test_resolve_site_id(sp_provider):
    _mock_token()
    respx.get(
        f"{GRAPH_BASE}/sites/corp.sharepoint.com:/sites/legal"
    ).mock(
        return_value=httpx.Response(200, json={
            "id": "site-id-123",
            "displayName": "Legal",
        })
    )

    site_id = await sp_provider.resolve_site_id("https://corp.sharepoint.com/sites/legal")
    assert site_id == "site-id-123"


@respx.mock
@pytest.mark.asyncio
async def test_delta_sync(sp_provider):
    _mock_token()
    respx.get(f"{GRAPH_BASE}/drives/d1/root/delta").mock(
        return_value=httpx.Response(200, json={
            "value": [
                {"id": "item1", "name": "doc.docx", "file": {}, "size": 1000,
                 "parentReference": {"path": "/drives/d1/root:"},
                 "lastModifiedDateTime": "2025-01-01T00:00:00Z",
                 "cTag": "ctag1"},
            ],
            "@odata.deltaLink": "https://graph.../delta?token=xyz",
        })
    )

    items, delta_link = await sp_provider.delta_sync("d1")
    assert len(items) == 1
    assert "token=xyz" in delta_link


class TestSharePointChanges:
    def test_extract_changes_deleted(self, sp_provider):
        items = [
            {"name": "deleted.docx", "deleted": {}, "parentReference": {"path": "/drives/d1/root:"}},
        ]
        config = SiteConfig(url="https://t.sharepoint.com/sites/s")
        changes = sp_provider.extract_changes(items, config)
        assert len(changes) == 1
        assert changes[0].status == "removed"

    def test_extract_changes_modified(self, sp_provider):
        items = [
            {"name": "updated.py", "file": {}, "parentReference": {"path": "/drives/d1/root:/src"}},
        ]
        config = SiteConfig(url="https://t.sharepoint.com/sites/s")
        changes = sp_provider.extract_changes(items, config)
        assert len(changes) == 1
        assert changes[0].status == "modified"
        assert changes[0].path == "src/updated.py"


# ── Outlook Tests ───────────────────────────────────────────


class TestOutlookHelpers:
    def test_extract_email_address(self):
        recipient = {"emailAddress": {"name": "Alice", "address": "alice@corp.com"}}
        assert _extract_email(recipient) == "alice@corp.com"

    def test_extract_email_no_address(self):
        recipient = {"emailAddress": {"name": "Alice"}}
        assert _extract_email(recipient) == "Alice"

    def test_extract_email_empty(self):
        assert _extract_email({}) == "unknown"


@respx.mock
@pytest.mark.asyncio
async def test_outlook_resolve_folder(outlook_provider):
    _mock_token()
    respx.get(f"{GRAPH_BASE}/users/user@corp.com/mailFolders/inbox").mock(
        return_value=httpx.Response(200, json={"id": "folder-123"})
    )

    folder_id = await outlook_provider.resolve_folder_id("user@corp.com", "Inbox")
    assert folder_id == "folder-123"


@pytest.mark.asyncio
async def test_outlook_message_to_document(outlook_provider):
    msg = {
        "id": "msg-1",
        "subject": "Test Subject",
        "from": {"emailAddress": {"address": "sender@corp.com"}},
        "toRecipients": [{"emailAddress": {"address": "recipient@corp.com"}}],
        "ccRecipients": [],
        "receivedDateTime": "2025-06-01T10:00:00Z",
        "body": {"contentType": "text", "content": "Hello, this is a test email."},
        "hasAttachments": False,
        "conversationId": "conv-1",
    }
    config = MailboxConfig(user="user@corp.com", folders=["Inbox"])
    doc = outlook_provider._message_to_document(msg, "user@corp.com", config, "test-source")
    assert doc is not None
    assert "Test Subject" in doc.content
    assert "Hello, this is a test" in doc.content
    assert doc.source_type == "email"
    assert doc.metadata["sender"] == "sender@corp.com"


@pytest.mark.asyncio
async def test_outlook_html_message(outlook_provider):
    msg = {
        "id": "msg-2",
        "subject": "HTML Email",
        "from": {"emailAddress": {"address": "sender@corp.com"}},
        "toRecipients": [],
        "ccRecipients": [],
        "receivedDateTime": "2025-06-01T10:00:00Z",
        "body": {"contentType": "html", "content": "<p>HTML <b>content</b></p>"},
        "hasAttachments": False,
        "conversationId": "conv-2",
    }
    config = MailboxConfig(user="user@corp.com", folders=["Inbox"])
    doc = outlook_provider._message_to_document(msg, "user@corp.com", config, "test")
    assert doc is not None
    assert "HTML content" in doc.content
    assert "<p>" not in doc.content


class TestOutlookChanges:
    def test_removed_message(self, outlook_provider):
        messages = [{"id": "msg-1", "@removed": {"reason": "deleted"}}]
        changes = outlook_provider.extract_changes(messages, "user@corp.com")
        assert len(changes) == 1
        assert changes[0].status == "removed"

    def test_modified_message(self, outlook_provider):
        messages = [{"id": "msg-2"}]
        changes = outlook_provider.extract_changes(messages, "user@corp.com")
        assert len(changes) == 1
        assert changes[0].status == "modified"


# ── Teams Tests ─────────────────────────────────────────────


class TestTeamsHelpers:
    def test_get_sender(self):
        msg = {"from": {"user": {"displayName": "Alice"}}}
        assert _get_sender(msg) == "Alice"

    def test_get_sender_fallback(self):
        msg = {"from": {"user": {"id": "user-id-123"}}}
        assert _get_sender(msg) == "user-id-123"

    def test_get_sender_empty(self):
        assert _get_sender({}) == "unknown"


@pytest.mark.asyncio
async def test_teams_thread_to_document(teams_provider):
    msg = {
        "id": "msg-1",
        "body": {"contentType": "text", "content": "Main message text"},
        "from": {"user": {"displayName": "Alice"}},
        "createdDateTime": "2025-06-01T10:00:00Z",
        "lastEditedDateTime": None,
        "deletedDateTime": None,
        "replyToId": None,
        "attachments": [],
        "reactions": [],
        "replies": [
            {
                "id": "reply-1",
                "body": {"contentType": "text", "content": "Reply from Bob"},
                "from": {"user": {"displayName": "Bob"}},
                "createdDateTime": "2025-06-01T10:05:00Z",
                "deletedDateTime": None,
            }
        ],
    }
    config = TeamConfig(name="Engineering", channels=["General"])
    doc = teams_provider._thread_to_document(msg, config, "General", "test-source")
    assert doc is not None
    assert "Main message text" in doc.content
    assert "Reply from Bob" in doc.content
    assert "Alice" in doc.content
    assert doc.source_type == "teams"
    assert doc.metadata["reply_count"] == 1


@pytest.mark.asyncio
async def test_teams_empty_message_returns_none(teams_provider):
    msg = {
        "id": "msg-2",
        "body": {"contentType": "text", "content": ""},
        "from": {"user": {"displayName": "Alice"}},
        "createdDateTime": "2025-06-01T10:00:00Z",
        "deletedDateTime": None,
        "replyToId": None,
        "attachments": [],
        "replies": [],
    }
    config = TeamConfig(name="Engineering", channels=["General"])
    doc = teams_provider._thread_to_document(msg, config, "General", "test")
    assert doc is None


class TestTeamsDeduplication:
    def test_file_attachments_extracted(self, teams_provider):
        msg = {
            "attachments": [
                {"contentType": "reference", "name": "report.docx"},
                {"contentType": "reference", "name": "data.xlsx"},
                {"contentType": "messageReference", "name": "forwarded"},
            ]
        }
        names = teams_provider._get_file_attachment_names(msg)
        assert names == ["report.docx", "data.xlsx"]

    def test_no_attachments(self, teams_provider):
        names = teams_provider._get_file_attachment_names({"attachments": []})
        assert names == []


class TestTeamsChanges:
    def test_deleted_message(self, teams_provider):
        messages = [
            {"id": "msg-1", "deletedDateTime": "2025-06-01T10:00:00Z", "replyToId": None}
        ]
        changes = teams_provider.extract_changes(messages, "Eng", "General")
        assert len(changes) == 1
        assert changes[0].status == "removed"

    def test_removed_message(self, teams_provider):
        messages = [{"id": "msg-2", "@removed": {}, "replyToId": None}]
        changes = teams_provider.extract_changes(messages, "Eng", "General")
        assert changes[0].status == "removed"

    def test_reply_not_tracked(self, teams_provider):
        messages = [{"id": "reply-1", "replyToId": "msg-1"}]
        changes = teams_provider.extract_changes(messages, "Eng", "General")
        assert len(changes) == 0
