"""Tests for the repository sync service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ingestion.sync_service import (
    load_repo_configs,
    sync_repo,
)
from ingestion.adapters.git_adapter import RepoConfig
from ingestion.adapters.base import NormalizedDocument, FileChange


@pytest.fixture
def repo_config():
    return RepoConfig(
        name="test-repo",
        url="https://github.com/org/test-repo",
        branch="main",
        collection="pb_general",
        project="test-project",
        classification="internal",
    )


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock(return_value="DELETE 0")
    return pool


@pytest.fixture
def mock_http():
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"status": "ok", "chunks_ingested": 1}
    client.post = AsyncMock(return_value=resp)
    return client


class TestLoadRepoConfigs:
    def test_load_empty(self, tmp_path):
        path = tmp_path / "repos.yaml"
        path.write_text("repos: []\n")
        configs = load_repo_configs(path)
        assert configs == []

    def test_load_missing_file(self, tmp_path):
        configs = load_repo_configs(tmp_path / "nonexistent.yaml")
        assert configs == []

    def test_load_valid(self, tmp_path):
        path = tmp_path / "repos.yaml"
        path.write_text("""
repos:
  - name: "my-repo"
    url: "https://github.com/org/repo"
    branch: "main"
    collection: "pb_general"
    project: "my-project"
    classification: "internal"
    include: ["docs/**"]
""")
        configs = load_repo_configs(path)
        assert len(configs) == 1
        assert configs[0].name == "my-repo"
        assert configs[0].include == ["docs/**"]


class TestSyncRepo:
    @patch("ingestion.sync_service.GitAdapter")
    async def test_first_sync(self, MockAdapter, repo_config, mock_pool, mock_http):
        adapter = AsyncMock()
        adapter.get_current_sha.return_value = "abc123"
        adapter.fetch_all_files.return_value = [
            NormalizedDocument(
                content="# Hello",
                content_type="markdown",
                source_ref="github:org/repo:README.md@abc123",
                source_type="github",
                metadata={"file_path": "README.md"},
            ),
        ]
        MockAdapter.return_value = adapter

        result = await sync_repo(repo_config, mock_pool, mock_http)

        assert result["status"] == "synced"
        assert result["sha"] == "abc123"
        assert result["ingested"] == 1
        adapter.fetch_all_files.assert_called_once()

    @patch("ingestion.sync_service.GitAdapter")
    async def test_already_synced(self, MockAdapter, repo_config, mock_pool, mock_http):
        # Simulate existing sync state with same SHA
        mock_pool.fetchrow.return_value = {
            "repo_name": "test-repo",
            "last_commit_sha": "abc123",
            "file_count": 5,
        }
        adapter = AsyncMock()
        adapter.get_current_sha.return_value = "abc123"
        MockAdapter.return_value = adapter

        result = await sync_repo(repo_config, mock_pool, mock_http)

        assert result["status"] == "up_to_date"
        adapter.fetch_all_files.assert_not_called()

    @patch("ingestion.sync_service.GitAdapter")
    async def test_incremental_sync(self, MockAdapter, repo_config, mock_pool, mock_http):
        mock_pool.fetchrow.return_value = {
            "repo_name": "test-repo",
            "last_commit_sha": "old_sha",
            "file_count": 5,
        }
        adapter = AsyncMock()
        adapter.get_current_sha.return_value = "new_sha"
        adapter.get_file_changes.return_value = [
            FileChange(path="new.py", status="added"),
            FileChange(path="deleted.py", status="removed"),
        ]
        adapter.fetch_changed_files.return_value = [
            NormalizedDocument(
                content="print('hello')",
                content_type="python",
                source_ref="github:org/repo:new.py@new_sha",
                source_type="github",
                metadata={"file_path": "new.py"},
            ),
        ]
        MockAdapter.return_value = adapter

        result = await sync_repo(repo_config, mock_pool, mock_http)

        assert result["status"] == "synced"
        assert result["ingested"] == 1
        adapter.get_file_changes.assert_called_once_with("old_sha")
        adapter.fetch_changed_files.assert_called_once_with("old_sha")

    @patch("ingestion.sync_service.GitAdapter")
    async def test_sync_error_preserves_sha(self, MockAdapter, repo_config, mock_pool, mock_http):
        adapter = AsyncMock()
        adapter.get_current_sha.side_effect = Exception("API down")
        MockAdapter.return_value = adapter

        result = await sync_repo(repo_config, mock_pool, mock_http)

        assert result["status"] == "error"
        assert "API down" in result["error"]
        # Verify error status was saved
        calls = mock_pool.execute.call_args_list
        # Last upsert should have status='error'
        assert any("error" in str(c) for c in calls)
