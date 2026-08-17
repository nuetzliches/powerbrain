"""Tests for graph_service CRUD with mocked asyncpg pool."""

import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from graph_service import (
    create_node, create_relationship, find_node, delete_node,
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


class TestCreateNodeIsIdempotent:
    """A repeated sync run must not add a second copy of the same node.

    Plain CREATE never checks uniqueness, so it never errors and every run
    silently inserts another copy of the same entity.
    """

    @staticmethod
    def _cypher(conn):
        """The cypher statement, unwrapped from the SELECT ... cypher(...) shell."""
        return conn.fetch.call_args[0][0]

    async def test_merges_on_id_and_sets_remaining_properties(self, mock_pool):
        pool, conn = mock_pool
        await create_node(pool, "Project", {"id": "proj-1", "name": "Project One", "status": "active"})

        cypher = self._cypher(conn)
        assert "MERGE (n:Project {id: 'proj-1'})" in cypher
        assert "CREATE" not in cypher
        # name/status must be SET, not part of the MERGE pattern -- otherwise a
        # renamed project would merge into a second node instead of updating.
        assert "SET n.name = 'Project One', n.status = 'active'" in cypher

    async def test_falls_back_to_name_when_no_id(self, mock_pool):
        pool, conn = mock_pool
        await create_node(pool, "Customer", {"name": "Acme"})

        assert "MERGE (n:Customer {name: 'Acme'})" in self._cypher(conn)

    async def test_merges_on_all_properties_without_identity_key(self, mock_pool):
        pool, conn = mock_pool
        await create_node(pool, "Concept", {"topic": "GDPR"})

        cypher = self._cypher(conn)
        assert "MERGE (n:Concept {topic: 'GDPR'})" in cypher
        assert "SET" not in cypher

    async def test_keeps_create_for_property_less_node(self, mock_pool):
        pool, conn = mock_pool
        await create_node(pool, "Project", {})

        # MERGE on a bare label would match any existing Project.
        assert "CREATE (n:Project) RETURN n" in self._cypher(conn)
        assert "MERGE" not in self._cypher(conn)

    async def test_logs_upsert_not_create(self, mock_pool):
        pool, conn = mock_pool
        await create_node(pool, "Project", {"id": "proj-1"})

        assert pool.execute.call_args[0][3] == "upsert"


class TestCreateRelationshipIsIdempotent:
    """MATCH ... CREATE grows worse than linear: on run N it matches N copies
    per side and creates N^2 edges, so edge count grows with the sum of k^2.
    """

    @staticmethod
    def _cypher(conn):
        return conn.fetch.call_args[0][0]

    async def test_merges_the_edge(self, mock_pool):
        pool, conn = mock_pool
        await create_relationship(
            pool, "Project", "proj-1", "Customer", "cust-1", "BELONGS_TO",
        )

        cypher = self._cypher(conn)
        assert "MERGE (a)-[r:BELONGS_TO]->(b)" in cypher
        assert "CREATE" not in cypher

    async def test_properties_are_set_not_merged(self, mock_pool):
        pool, conn = mock_pool
        await create_relationship(
            pool, "Project", "proj-1", "Customer", "cust-1", "BELONGS_TO",
            properties={"since": "2026-04-21"},
        )

        cypher = self._cypher(conn)
        # A property inside the MERGE pattern would make every changed value
        # create an additional edge -- exactly the duplication we are fixing.
        assert "MERGE (a)-[r:BELONGS_TO]->(b) SET r.since = '2026-04-21'" in cypher

    async def test_rejects_invalid_rel_type(self, mock_pool):
        pool, conn = mock_pool
        with pytest.raises(ValueError, match="rel_type"):
            await create_relationship(pool, "Project", "a", "Customer", "b", "BAD-TYPE")


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
