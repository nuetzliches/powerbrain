"""Tests for ingestion/snapshot_service.py — knowledge versioning."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import snapshot_service
from snapshot_service import (
    create_qdrant_snapshots,
    list_qdrant_snapshots,
    delete_qdrant_snapshot,
    get_pg_row_counts,
    get_policy_commit,
)


# ── Fixtures ──────────────────────────────────────────────


def _mock_http():
    client = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {}
    client.post.return_value = resp
    client.get.return_value = resp
    client.delete.return_value = resp
    return client, resp


def _mock_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    pool.fetch.return_value = []
    pool.execute.return_value = "DELETE 0"
    return pool


# ── Qdrant Snapshots ─────────────────────────────────────


class TestCreateQdrantSnapshots:
    async def test_success(self):
        client, resp = _mock_http()
        resp.json.return_value = {"result": {"name": "snap-2026"}}
        resp.status_code = 200

        result = await create_qdrant_snapshots(client)
        assert len(result) == 3
        assert all(v == "snap-2026" for v in result.values())
        assert client.post.call_count == 3

    async def test_collection_not_found_skipped(self):
        client, resp = _mock_http()
        resp.status_code = 404
        resp.json.return_value = {"result": {"name": "x"}}

        result = await create_qdrant_snapshots(client)
        assert result == {}

    async def test_error_logged_and_continues(self):
        client, _ = _mock_http()
        client.post.side_effect = httpx.ConnectError("qdrant down")

        result = await create_qdrant_snapshots(client)
        assert result == {}


class TestListQdrantSnapshots:
    async def test_returns_list(self):
        client, resp = _mock_http()
        resp.json.return_value = {"result": [{"name": "s1"}, {"name": "s2"}]}

        result = await list_qdrant_snapshots(client, "pb_general")
        assert len(result) == 2

    async def test_error_returns_empty(self):
        client, _ = _mock_http()
        client.get.side_effect = RuntimeError("fail")

        result = await list_qdrant_snapshots(client, "pb_general")
        assert result == []


class TestDeleteQdrantSnapshot:
    async def test_calls_delete(self):
        client, resp = _mock_http()
        await delete_qdrant_snapshot(client, "pb_general", "snap-1")
        client.delete.assert_called_once()
        url = client.delete.call_args[0][0]
        assert "pb_general" in url
        assert "snap-1" in url


# ── PostgreSQL Row Counts ────────────────────────────────


class TestPgRowCounts:
    async def test_returns_counts(self):
        pool = _mock_pool()
        pool.fetchrow.return_value = {"cnt": 42}

        result = await get_pg_row_counts(pool)
        assert all(v == 42 for v in result.values())
        assert "datasets" in result

    async def test_missing_table_returns_minus_one(self):
        pool = _mock_pool()
        pool.fetchrow.side_effect = Exception("relation does not exist")

        result = await get_pg_row_counts(pool)
        assert all(v == -1 for v in result.values())


# ── Policy Commit ────────────────────────────────────────


class TestPolicyCommit:
    async def test_returns_commit_hash(self, monkeypatch):
        monkeypatch.setattr(snapshot_service, "FORGEJO_TOKEN", "tok123")
        client, resp = _mock_http()
        resp.json.return_value = {"commit": {"id": "abc123def"}}

        result = await get_policy_commit(client)
        assert result == "abc123def"

    async def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.setattr(snapshot_service, "FORGEJO_TOKEN", "")
        client, _ = _mock_http()

        result = await get_policy_commit(client)
        assert result is None

    async def test_api_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(snapshot_service, "FORGEJO_TOKEN", "tok")
        client, _ = _mock_http()
        client.get.side_effect = httpx.ConnectError("forgejo down")

        result = await get_policy_commit(client)
        assert result is None


# ── Create Snapshot (orchestration) ──────────────────────


class TestCreateSnapshot:
    async def test_creates_full_snapshot(self, monkeypatch):
        mock_pool = _mock_pool()
        mock_pool.fetchrow.side_effect = [
            # get_pg_row_counts calls (3 tables)
            {"cnt": 10}, {"cnt": 20}, {"cnt": 30},
            # create_snapshot INSERT RETURNING
            {"id": 1, "created_at": datetime(2026, 4, 10, tzinfo=timezone.utc)},
        ]

        async def _fake_create_pool(*a, **kw):
            return mock_pool

        monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)
        monkeypatch.setattr(snapshot_service, "FORGEJO_TOKEN", "")

        # Mock httpx.AsyncClient context manager
        mock_client, resp = _mock_http()
        resp.json.return_value = {"result": {"name": "snap-1"}}
        resp.status_code = 200

        original_init = httpx.AsyncClient.__init__

        class FakeClient:
            def __init__(self, **kw):
                pass
            async def __aenter__(self):
                return mock_client
            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr("httpx.AsyncClient", FakeClient)

        result = await snapshot_service.create_snapshot("test-snap", description="test")
        assert result["name"] == "test-snap"
        assert result["snapshot_id"] == 1
        assert "components" in result


# ── Cleanup Old Snapshots ────────────────────────────────


class TestCleanupOldSnapshots:
    async def test_deletes_old_snapshots(self, monkeypatch):
        mock_pool = _mock_pool()
        mock_pool.fetch.return_value = [
            {"id": 1, "snapshot_name": "old-snap", "components": {
                "qdrant": {"collections": {"pb_general": "snap-old"}},
            }},
        ]

        async def _fake_create_pool(*a, **kw):
            return mock_pool

        monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)

        mock_client, resp = _mock_http()

        class FakeClient:
            def __init__(self, **kw):
                pass
            async def __aenter__(self):
                return mock_client
            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr("httpx.AsyncClient", FakeClient)

        await snapshot_service.cleanup_old_snapshots(keep_last_n=5)
        mock_client.delete.assert_called_once()
        mock_pool.execute.assert_called_once()

    async def test_no_old_snapshots(self, monkeypatch):
        mock_pool = _mock_pool()
        mock_pool.fetch.return_value = []

        async def _fake_create_pool(*a, **kw):
            return mock_pool

        monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)

        mock_client, _ = _mock_http()

        class FakeClient:
            def __init__(self, **kw):
                pass
            async def __aenter__(self):
                return mock_client
            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr("httpx.AsyncClient", FakeClient)

        await snapshot_service.cleanup_old_snapshots(keep_last_n=5)
        mock_client.delete.assert_not_called()
