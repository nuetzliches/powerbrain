"""Tests for ingestion/retention_cleanup.py — GDPR retention and deletion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from retention_cleanup import (
    get_expiring_data,
    delete_dataset,
    process_deletion_requests,
    clean_expired_vault,
    anonymize_old_audit_logs,
)


# ── Fixtures ──────────────────────────────────────────────


def _mock_pool():
    pool = AsyncMock()
    pool.fetch.return_value = []
    pool.fetchval.return_value = 0
    pool.fetchrow.return_value = None
    pool.execute.return_value = "DELETE 0"

    conn = AsyncMock()
    conn.fetch.return_value = []
    conn.execute.return_value = "DELETE 0"
    pool.acquire.return_value.__aenter__.return_value = conn

    return pool


def _mock_qdrant():
    qdrant = AsyncMock()
    qdrant.scroll.return_value = ([], None)
    qdrant.delete.return_value = None
    return qdrant


# ── get_expiring_data ────────────────────────────────────


class TestGetExpiringData:
    async def test_returns_expired_records(self):
        pool = _mock_pool()
        row = MagicMock()
        row.items.return_value = [
            ("source_type", "dataset"), ("id", "d1"), ("title", "test"),
        ]
        # asyncpg Record supports dict() conversion
        pool.fetch.return_value = [{"source_type": "dataset", "id": "d1", "title": "test"}]

        result = await get_expiring_data(pool)
        assert len(result) == 1
        assert result[0]["source_type"] == "dataset"
        pool.fetch.assert_called_once()

    async def test_empty_result(self):
        pool = _mock_pool()
        result = await get_expiring_data(pool)
        assert result == []


# ── delete_dataset ────────────────────────────────────────


class TestDeleteDataset:
    async def test_execute_deletes_qdrant_and_pg(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()

        doc_row = {"qdrant_collection": "pb_general"}
        pool.fetchrow.return_value = doc_row

        point = MagicMock()
        point.id = "p1"
        qdrant.scroll.return_value = ([point], None)

        report = await delete_dataset(pool, qdrant, "ds1", execute=True)
        assert any("deleted" in a.lower() for a in report["actions"])
        qdrant.delete.assert_called_once()
        # 3 PG DELETEs: dataset_rows, documents_meta, datasets + 1 UPDATE
        assert pool.execute.call_count >= 3

    async def test_dry_run_counts_only(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()
        pool.fetchrow.return_value = {"qdrant_collection": "pb_general"}
        pool.fetchval.return_value = 42

        report = await delete_dataset(pool, qdrant, "ds1", execute=False)
        assert any("DRY-RUN" in a for a in report["actions"])
        assert any("42" in a for a in report["actions"])
        qdrant.scroll.assert_not_called()

    async def test_no_qdrant_collection(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()
        pool.fetchrow.return_value = {"qdrant_collection": None}

        report = await delete_dataset(pool, qdrant, "ds1", execute=True)
        qdrant.scroll.assert_not_called()

    async def test_qdrant_error_in_report(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()
        pool.fetchrow.return_value = {"qdrant_collection": "pb_general"}
        qdrant.scroll.side_effect = RuntimeError("connection refused")

        report = await delete_dataset(pool, qdrant, "ds1", execute=True)
        assert any("error" in a.lower() for a in report["actions"])

    async def test_audit_anonymization(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()
        pool.fetchrow.return_value = {"qdrant_collection": None}

        await delete_dataset(pool, qdrant, "ds1", execute=True)
        # Check that agent_access_log UPDATE was called
        calls = [str(c) for c in pool.execute.call_args_list]
        assert any("agent_access_log" in c for c in calls)


# ── process_deletion_requests ────────────────────────────


class TestProcessDeletionRequests:
    async def test_no_pending_requests(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()
        result = await process_deletion_requests(pool, qdrant, execute=True)
        assert result == []

    async def test_dry_run_mode(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()
        pool.fetch.return_value = [{
            "id": "r1", "data_subject_id": "s1", "external_ref": "user@example.com",
            "datasets": ["d1"], "qdrant_point_ids": None,
        }]

        reports = await process_deletion_requests(pool, qdrant, execute=False)
        assert len(reports) == 1
        assert any("DRY-RUN" in a for a in reports[0]["actions"])

    async def test_malformed_qdrant_point_ids(self):
        pool = _mock_pool()
        qdrant = _mock_qdrant()
        pool.fetch.side_effect = [
            # First call: deletion requests
            [{
                "id": "r1", "data_subject_id": "s1", "external_ref": "ref",
                "datasets": None, "qdrant_point_ids": ["invalid_no_colon"],
            }],
        ]

        reports = await process_deletion_requests(pool, qdrant, execute=True)
        # Malformed point ID should be silently skipped
        qdrant.delete.assert_not_called()


# ── clean_expired_vault ──────────────────────────────────


class TestCleanExpiredVault:
    async def test_expired_entries_deleted(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [
            # Expired entries
            [{"id": "v1", "document_id": "d1", "chunk_index": 0}],
            # Orphaned entries
            [],
        ]
        conn.execute.return_value = "DELETE 1"

        stats = await clean_expired_vault(conn, dry_run=False)
        assert stats["expired_content"] == 1
        assert conn.execute.call_count >= 2  # mapping DELETE + content DELETE

    async def test_orphaned_entries_deleted(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [
            # No expired
            [],
            # Orphaned entries
            [{"id": "v2", "document_id": "d2"}],
        ]
        conn.execute.return_value = "DELETE 1"

        stats = await clean_expired_vault(conn, dry_run=False)
        assert stats["orphaned"] == 1

    async def test_dry_run_counts_only(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [
            [{"id": "v1", "document_id": "d1", "chunk_index": 0}],
            [{"id": "v2", "document_id": "d2"}],
        ]

        stats = await clean_expired_vault(conn, dry_run=True)
        assert stats["expired_content"] == 1
        assert stats["orphaned"] == 1
        conn.execute.assert_not_called()

    async def test_no_expired_or_orphaned(self):
        conn = AsyncMock()
        conn.fetch.return_value = []

        stats = await clean_expired_vault(conn, dry_run=False)
        assert stats == {"expired_content": 0, "expired_mappings": 0, "orphaned": 0}


# ── anonymize_old_audit_logs ─────────────────────────────


class TestAnonymizeOldAuditLogs:
    async def test_execute_anonymizes(self):
        pool = _mock_pool()
        pool.fetchval.return_value = 15

        report = await anonymize_old_audit_logs(pool, execute=True)
        assert report["entries_to_anonymize"] == 15
        pool.execute.assert_called_once()
        assert any("anonymized" in a.lower() for a in report["actions"])

    async def test_dry_run_reports_count(self):
        pool = _mock_pool()
        pool.fetchval.return_value = 5

        report = await anonymize_old_audit_logs(pool, execute=False)
        assert report["entries_to_anonymize"] == 5
        pool.execute.assert_not_called()
        assert any("DRY-RUN" in a for a in report["actions"])

    async def test_nothing_to_anonymize(self):
        pool = _mock_pool()
        pool.fetchval.return_value = 0

        report = await anonymize_old_audit_logs(pool, execute=True)
        assert report["entries_to_anonymize"] == 0
        pool.execute.assert_not_called()
