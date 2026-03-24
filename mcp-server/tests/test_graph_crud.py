"""Tests for graph_service CRUD with mocked asyncpg pool."""

import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from graph_service import (
    create_node, find_node, delete_node,
    _execute_cypher, validate_identifier,
)


class _AsyncContextManager:
    """Helper that acts as an async context manager returning a mock connection."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def mock_pool():
    """Create a mock pool where pool.acquire() returns an async context manager.

    asyncpg's Pool.acquire() is a regular (non-async) method that returns
    a PoolAcquireContext (an async context manager).  We replicate this
    with MagicMock for acquire + a helper _AsyncContextManager.
    """
    pool = MagicMock()
    conn = AsyncMock()
    conn.execute.return_value = None
    conn.fetch.return_value = []
    # pool.acquire() is a regular call returning an async CM
    pool.acquire.return_value = _AsyncContextManager(conn)
    # Also expose pool.execute as AsyncMock for _log_sync calls
    pool.execute = AsyncMock()
    return pool, conn


class TestCreateNode:
    async def test_creates_node_with_properties(self, mock_pool):
        pool, conn = mock_pool
        conn.fetch.return_value = [{"n": '{"id": 1, "properties": {"name": "Test"}}'}]

        result = await create_node(pool, "Project", {"name": "Test"})

        assert result.get("id") == 1 or result.get("properties", {}).get("name") == "Test"
        assert conn.fetch.called

    async def test_rejects_invalid_label(self, mock_pool):
        pool, conn = mock_pool
        with pytest.raises(ValueError, match="Label"):
            await create_node(pool, "invalid-label", {"name": "x"})

    async def test_rejects_invalid_property_key(self, mock_pool):
        pool, conn = mock_pool
        with pytest.raises(ValueError, match="Property-Key"):
            await create_node(pool, "Project", {"invalid key": "x"})


class TestFindNode:
    async def test_returns_matching_nodes(self, mock_pool):
        pool, conn = mock_pool
        conn.fetch.return_value = [
            {"n": '{"id": 1, "properties": {"name": "A"}}'},
            {"n": '{"id": 2, "properties": {"name": "B"}}'},
        ]

        result = await find_node(pool, "Project", {"name": "A"})
        assert len(result) == 2

    async def test_empty_properties_matches_all(self, mock_pool):
        pool, conn = mock_pool
        conn.fetch.return_value = []

        result = await find_node(pool, "Project", {})
        assert result == []
        assert conn.fetch.called


class TestDeleteNode:
    async def test_returns_true(self, mock_pool):
        pool, conn = mock_pool
        result = await delete_node(pool, "Project", "node-1")
        assert result is True

    async def test_rejects_invalid_label(self, mock_pool):
        pool, conn = mock_pool
        with pytest.raises(ValueError):
            await delete_node(pool, "bad label", "node-1")


class TestExecuteCypher:
    async def test_parses_agtype_result(self, mock_pool):
        pool, conn = mock_pool
        conn.fetch.return_value = [
            {"n": '{"id": 1, "label": "Project", "properties": {"name": "X"}}::vertex'}
        ]

        result = await _execute_cypher(pool, "MATCH (n) RETURN n")
        assert len(result) == 1
        assert result[0].get("id") == 1

    async def test_handles_parse_error_gracefully(self, mock_pool):
        pool, conn = mock_pool
        conn.fetch.return_value = [{"n": "not-json"}]

        result = await _execute_cypher(pool, "MATCH (n) RETURN n")
        assert len(result) == 1
        assert "raw" in result[0]
