"""Tests for the Office 365 adapter (SourceAdapter implementation)."""

from __future__ import annotations

import pytest

from ingestion.adapters.office365.adapter import Office365Adapter, Office365Config


# ── Config Tests ────────────────────────────────────────────


class TestOffice365Config:
    def test_defaults(self):
        config = Office365Config(
            name="test",
            tenant_id="t1",
            client_id="c1",
            client_secret="s1",
        )
        assert config.collection == "pb_general"
        assert config.poll_interval_minutes == 15
        assert config.max_file_size_mb == 50
        assert config.sites == []
        assert config.mailboxes == []
        assert config.teams == []
        assert config.onenote == []

    def test_site_configs(self):
        config = Office365Config(
            name="test",
            tenant_id="t1",
            client_id="c1",
            client_secret="s1",
            sites=[
                {"url": "https://t.sharepoint.com/sites/legal", "classification": "confidential"},
                {"url": "https://t.sharepoint.com/sites/wiki", "classification": "internal"},
            ],
        )
        site_cfgs = config.site_configs
        assert len(site_cfgs) == 2
        assert site_cfgs[0].url == "https://t.sharepoint.com/sites/legal"
        assert site_cfgs[0].classification == "confidential"
        assert site_cfgs[1].classification == "internal"

    def test_mailbox_configs(self):
        config = Office365Config(
            name="test",
            tenant_id="t1",
            client_id="c1",
            client_secret="s1",
            mailboxes=[
                {"user": "support@corp.com", "folders": ["Inbox"], "classification": "confidential"},
            ],
        )
        mb_cfgs = config.mailbox_configs
        assert len(mb_cfgs) == 1
        assert mb_cfgs[0].user == "support@corp.com"
        assert mb_cfgs[0].classification == "confidential"

    def test_team_configs(self):
        config = Office365Config(
            name="test",
            tenant_id="t1",
            client_id="c1",
            client_secret="s1",
            teams=[
                {"name": "Engineering", "channels": ["General", "Architecture"]},
            ],
        )
        team_cfgs = config.team_configs
        assert len(team_cfgs) == 1
        assert team_cfgs[0].name == "Engineering"
        assert team_cfgs[0].channels == ["General", "Architecture"]

    def test_onenote_configs(self):
        config = Office365Config(
            name="test",
            tenant_id="t1",
            client_id="c1",
            client_secret="s1",
            onenote=[
                {"notebook": "Team Wiki", "site": "https://t.sharepoint.com/sites/wiki"},
            ],
        )
        on_cfgs = config.onenote_configs
        assert len(on_cfgs) == 1
        assert on_cfgs[0].notebook == "Team Wiki"
        assert on_cfgs[0].site == "https://t.sharepoint.com/sites/wiki"


# ── Adapter Interface Tests ─────────────────────────────────


class TestOffice365Adapter:
    @pytest.fixture
    def config(self):
        return Office365Config(
            name="test-source",
            tenant_id="test-tenant",
            client_id="test-client",
            client_secret="test-secret",
            project="test-project",
        )

    @pytest.fixture
    def adapter(self, config):
        import httpx
        return Office365Adapter(config, httpx.AsyncClient())

    @pytest.mark.asyncio
    async def test_get_current_sha_returns_timestamp(self, adapter):
        sha = await adapter.get_current_sha()
        # Should be an ISO timestamp
        assert "T" in sha
        assert ":" in sha

    def test_delta_links_property(self, adapter):
        assert adapter.delta_links == {}
        adapter.delta_links = {"drive:d1": "https://link"}
        assert adapter.delta_links == {"drive:d1": "https://link"}

    def test_adapter_has_all_providers(self, adapter):
        assert adapter._sharepoint is not None
        assert adapter._outlook is not None
        assert adapter._teams is not None
        assert adapter._onenote is not None
        assert adapter._extractor is not None
        assert adapter._graph is not None

    def test_adapter_inherits_source_adapter(self, adapter):
        from ingestion.adapters.base import SourceAdapter
        assert isinstance(adapter, SourceAdapter)
