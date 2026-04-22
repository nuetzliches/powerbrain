"""Regression tests for issue #59 part 1 — audit hash chain race.

Before the fix (init-db/022_audit_tail_pointer.sql), concurrent INSERTs
into agent_access_log could fork the SHA-256 hash chain because the
BEFORE INSERT trigger's SELECT read from the parent statement's stale
snapshot even after the advisory lock was granted. pb_verify_audit_chain()
then reported valid=false after only a few thousand concurrent writes.

These tests write N audit rows from M concurrent asyncpg tasks and
verify the chain is intact end-to-end. They require a running Postgres
with migrations applied — gated behind RUN_INTEGRATION_TESTS=1.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql://pb_admin:changeme@localhost:5432/powerbrain",
)


@pytest.fixture
async def pool():
    """Asyncpg pool sized for concurrency tests."""
    p = await asyncpg.create_pool(POSTGRES_URL, min_size=4, max_size=32)
    try:
        yield p
    finally:
        await p.close()


async def _insert_audit_rows(pool: asyncpg.Pool, agent_id: str, count: int) -> None:
    """Write `count` audit rows sequentially from one task."""
    for i in range(count):
        await pool.execute(
            """
            INSERT INTO agent_access_log
                (agent_id, agent_role, resource_type, resource_id,
                 action, policy_result, request_context, contains_pii)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            agent_id, "analyst",
            "test", f"{agent_id}-{i}",
            "read", "allow", "{}", False,
        )


class TestAuditChainIntegrity:
    """Heavy concurrent writes must leave the chain verifiable."""

    async def test_sequential_writer_chain_valid(self, pool):
        baseline = await pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert baseline["valid"] is True

        await _insert_audit_rows(pool, f"seq-{uuid.uuid4().hex[:8]}", 50)

        after = await pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert after["valid"] is True, (
            f"chain broke after sequential inserts: first_invalid_id="
            f"{after['first_invalid_id']}"
        )
        assert after["total_checked"] >= baseline["total_checked"] + 50

    async def test_16_concurrent_writers_100_rows_each(self, pool):
        """The exact shape reported in issue #59: concurrency=8+, thousands of rows."""
        baseline = await pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert baseline["valid"] is True

        writers = 16
        rows_per_writer = 100
        agent_prefix = uuid.uuid4().hex[:8]

        await asyncio.gather(*[
            _insert_audit_rows(pool, f"conc-{agent_prefix}-{w}", rows_per_writer)
            for w in range(writers)
        ])

        result = await pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert result["valid"] is True, (
            f"chain forked under concurrency: first_invalid_id="
            f"{result['first_invalid_id']}, checked={result['total_checked']}"
        )
        assert result["total_checked"] >= baseline["total_checked"] + writers * rows_per_writer

    async def test_audit_tail_pointer_stays_in_sync(self, pool):
        """audit_tail.last_entry_hash must match the newest row's entry_hash."""
        await _insert_audit_rows(pool, f"tail-{uuid.uuid4().hex[:8]}", 20)

        tail = await pool.fetchrow("SELECT last_entry_hash, last_entry_id FROM audit_tail WHERE id = 1")
        newest = await pool.fetchrow(
            "SELECT id, entry_hash FROM agent_access_log ORDER BY id DESC LIMIT 1"
        )

        assert tail["last_entry_id"] == newest["id"]
        assert bytes(tail["last_entry_hash"]) == bytes(newest["entry_hash"])

    async def test_prune_does_not_break_chain(self, pool):
        """pb_audit_checkpoint_and_prune must hold the tail lock.

        Runs a prune with retention_days=365 (no rows actually eligible in
        most test environments) just to exercise the lock-acquisition path
        and verify the chain is still valid afterwards.
        """
        await _insert_audit_rows(pool, f"prune-{uuid.uuid4().hex[:8]}", 10)

        await pool.fetchrow("SELECT * FROM pb_audit_checkpoint_and_prune(365)")

        result = await pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert result["valid"] is True
