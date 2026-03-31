"""Tests for the delete_documents MCP tool."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server import _build_delete_filter, _dispatch


# ── Helper fixtures ──────────────────────────────────────────

@pytest.fixture
def mock_qdrant():
    """Mock AsyncQdrantClient with count and delete."""
    client = AsyncMock()
    count_result = MagicMock()
    count_result.count = 0
    client.count.return_value = count_result
    client.delete.return_value = None
    return client


@pytest.fixture
def mock_pool():
    """Mock asyncpg pool for direct pool usage (not acquire-based)."""
    pool = AsyncMock()
    pool.fetch.return_value = []
    pool.fetchval.return_value = 0
    pool.execute.return_value = "DELETE 0"
    return pool


@pytest.fixture
def patch_server(mock_qdrant, mock_pool):
    """Patch server globals: qdrant, get_pg_pool, check_opa_policy, graph."""
    with patch("server.qdrant", mock_qdrant), \
         patch("server.get_pg_pool", AsyncMock(return_value=mock_pool)), \
         patch("server.check_opa_policy", AsyncMock(return_value={"allowed": True})) as mock_opa, \
         patch("server.graph") as mock_graph, \
         patch("server.log_access", AsyncMock()) as mock_log:
        mock_graph.delete_node = AsyncMock(return_value=True)
        yield {
            "qdrant": mock_qdrant,
            "pool": mock_pool,
            "opa": mock_opa,
            "graph": mock_graph,
            "log_access": mock_log,
        }


def _parse_result(result):
    """Extract JSON from TextContent list."""
    assert len(result) == 1
    return json.loads(result[0].text)


# ── Validation tests ─────────────────────────────────────────

class TestDeleteDocumentsValidation:
    async def test_confirm_required(self, patch_server):
        result = await _dispatch(
            "delete_documents",
            {"confirm": False, "source_type": "timesheet"},
            "agent-1", "admin",
        )
        data = _parse_result(result)
        assert "error" in data
        assert "confirm" in data["error"].lower()

    async def test_filter_required_without_delete_all(self, patch_server):
        result = await _dispatch(
            "delete_documents",
            {"confirm": True},
            "agent-1", "admin",
        )
        data = _parse_result(result)
        assert "error" in data
        assert "filter" in data["error"].lower()

    async def test_opa_deny_for_viewer(self, patch_server):
        patch_server["opa"].return_value = {"allowed": False}
        result = await _dispatch(
            "delete_documents",
            {"confirm": True, "source_type": "timesheet"},
            "agent-1", "viewer",
        )
        data = _parse_result(result)
        assert "error" in data
        assert "denied" in data["error"].lower()
        # Verify deny was logged
        patch_server["log_access"].assert_called_once()
        assert patch_server["log_access"].call_args[0][5] == "deny"


# ── Successful deletion tests ────────────────────────────────

class TestDeleteDocumentsSuccess:
    async def test_delete_by_source_type(self, patch_server):
        # Setup: 2 documents in PG, 5 vectors in pb_general
        patch_server["pool"].fetch.return_value = [
            {"id": "aaa-111"}, {"id": "bbb-222"},
        ]
        patch_server["pool"].fetchval.return_value = 1  # 1 vault entry

        count_result = MagicMock()
        count_result.count = 5
        patch_server["qdrant"].count.return_value = count_result

        result = await _dispatch(
            "delete_documents",
            {"confirm": True, "source_type": "timesheet"},
            "agent-1", "developer",
        )
        data = _parse_result(result)

        assert data["deleted"]["documents_meta"] == 2
        assert data["deleted"]["vault_entries"] == 1
        assert data["deleted"]["qdrant"]["pb_general"] == 5
        assert data["deleted"]["qdrant"]["pb_code"] == 5
        assert data["deleted"]["qdrant"]["pb_rules"] == 5
        assert data["filters"]["source_type"] == "timesheet"
        assert "errors" not in data

        # Verify Qdrant delete called for all 3 collections
        assert patch_server["qdrant"].delete.call_count == 3

        # Verify PG delete was called
        patch_server["pool"].execute.assert_called()

        # Verify graph delete called for each doc_id
        assert patch_server["graph"].delete_node.call_count == 2

    async def test_delete_by_project(self, patch_server):
        patch_server["pool"].fetch.return_value = [{"id": "ccc-333"}]
        patch_server["pool"].fetchval.return_value = 0

        count_result = MagicMock()
        count_result.count = 3
        patch_server["qdrant"].count.return_value = count_result

        result = await _dispatch(
            "delete_documents",
            {"confirm": True, "project": "PROJ-A"},
            "agent-1", "admin",
        )
        data = _parse_result(result)

        assert data["deleted"]["documents_meta"] == 1
        assert data["filters"]["project"] == "PROJ-A"
        assert data["filters"]["source_type"] is None

    async def test_delete_all(self, patch_server):
        patch_server["pool"].fetch.return_value = [
            {"id": "aaa-111"}, {"id": "bbb-222"}, {"id": "ccc-333"},
        ]
        patch_server["pool"].fetchval.return_value = 2

        count_result = MagicMock()
        count_result.count = 10
        patch_server["qdrant"].count.return_value = count_result

        result = await _dispatch(
            "delete_documents",
            {"confirm": True, "delete_all": True},
            "agent-1", "admin",
        )
        data = _parse_result(result)

        assert data["deleted"]["documents_meta"] == 3
        assert data["deleted"]["vault_entries"] == 2
        assert data["filters"]["delete_all"] is True

    async def test_no_matches_returns_zeroes(self, patch_server):
        # Default: pool.fetch returns [], qdrant.count returns 0
        result = await _dispatch(
            "delete_documents",
            {"confirm": True, "source_type": "nonexistent"},
            "agent-1", "developer",
        )
        data = _parse_result(result)

        assert data["deleted"]["documents_meta"] == 0
        assert data["deleted"]["vault_entries"] == 0
        assert data["deleted"]["qdrant"]["pb_general"] == 0
        assert data["deleted"]["graph_nodes"] == 0
        assert "errors" not in data


# ── Partial failure tests ────────────────────────────────────

class TestDeleteDocumentsPartialFailure:
    async def test_qdrant_partial_failure(self, patch_server):
        patch_server["pool"].fetch.return_value = [{"id": "aaa-111"}]
        patch_server["pool"].fetchval.return_value = 0

        call_count = 0

        async def count_side_effect(collection_name, count_filter, exact):
            nonlocal call_count
            call_count += 1
            if collection_name == "pb_code":
                raise ConnectionError("Qdrant connection lost")
            r = MagicMock()
            r.count = 3
            return r

        patch_server["qdrant"].count.side_effect = count_side_effect

        result = await _dispatch(
            "delete_documents",
            {"confirm": True, "source_type": "timesheet"},
            "agent-1", "admin",
        )
        data = _parse_result(result)

        # pb_general and pb_rules should succeed, pb_code should fail
        assert data["deleted"]["qdrant"]["pb_general"] == 3
        assert data["deleted"]["qdrant"]["pb_code"] == 0
        assert data["deleted"]["qdrant"]["pb_rules"] == 3
        assert "errors" in data
        assert any("pb_code" in e for e in data["errors"])

    async def test_graph_node_missing(self, patch_server):
        patch_server["pool"].fetch.return_value = [
            {"id": "aaa-111"}, {"id": "bbb-222"},
        ]
        patch_server["pool"].fetchval.return_value = 0

        call_count = 0

        async def graph_side_effect(pool, label, node_id):
            nonlocal call_count
            call_count += 1
            if node_id == "bbb-222":
                raise Exception("Node not found")
            return True

        patch_server["graph"].delete_node.side_effect = graph_side_effect

        count_result = MagicMock()
        count_result.count = 0
        patch_server["qdrant"].count.return_value = count_result

        result = await _dispatch(
            "delete_documents",
            {"confirm": True, "source_type": "timesheet"},
            "agent-1", "admin",
        )
        data = _parse_result(result)

        # Only 1 of 2 graph nodes successfully deleted
        assert data["deleted"]["graph_nodes"] == 1


# ── Helper function tests ────────────────────────────────────

class TestBuildDeleteFilter:
    def test_source_type_filter(self):
        qf, where, params = _build_delete_filter("timesheet", None, False)
        assert qf is not None
        assert len(qf.must) == 1
        assert "source_type" in where
        assert params == ["timesheet"]

    def test_project_filter(self):
        qf, where, params = _build_delete_filter(None, "PROJ-A", False)
        assert qf is not None
        assert len(qf.must) == 1
        assert "project" in where
        assert params == ["PROJ-A"]

    def test_combined_filter(self):
        qf, where, params = _build_delete_filter("timesheet", "PROJ-A", False)
        assert qf is not None
        assert len(qf.must) == 2
        assert params == ["timesheet", "PROJ-A"]
        assert "$1" in where and "$2" in where

    def test_delete_all_returns_none_filter(self):
        qf, where, params = _build_delete_filter(None, None, True)
        assert qf is None
        assert where == "1=1"
        assert params == []

    def test_no_filters_returns_none(self):
        qf, where, params = _build_delete_filter(None, None, False)
        assert qf is None
        assert where == "1=1"
        assert params == []
