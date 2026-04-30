"""Tests for B-40 Audit Hash-Chain MCP tools (verify_audit_integrity, export_audit_log).

These tests use mocked DB and OPA so they run without Docker. The actual
PostgreSQL trigger + verify + checkpoint functions are exercised end-to-end
by the E2E smoke tests (tests/integration/e2e/).
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import server
from server import _dispatch


@pytest.fixture(autouse=True)
def _patch_globals(monkeypatch):
    mock_http = AsyncMock()
    mock_pool = AsyncMock()
    monkeypatch.setattr(server, "http", mock_http)
    monkeypatch.setattr(server, "pg_pool", mock_pool)

    # get_pg_pool() reads global — stub it to return the mock directly.
    async def _fake_get_pool():
        return mock_pool
    monkeypatch.setattr(server, "get_pg_pool", _fake_get_pool)

    # log_access would attempt to hit ingestion — replace with no-op.
    async def _noop_log(*args, **kwargs):
        return None
    monkeypatch.setattr(server, "log_access", _noop_log)

    return mock_http, mock_pool


def mock_row(mapping: dict):
    """Build a MagicMock supporting row[key] subscripting."""
    row = MagicMock()
    row.__getitem__ = lambda self, k: mapping[k]
    return row


def _verify_row(valid=True, first_invalid_id=None, total_checked=3,
                last_valid_hash="a" * 64):
    return mock_row({
        "valid": valid,
        "first_invalid_id": first_invalid_id,
        "total_checked": total_checked,
        "last_valid_hash": last_valid_hash,
    })


class TestVerifyAuditIntegrity:
    async def test_admin_happy_path(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _verify_row(valid=True, total_checked=42)

        result = await _dispatch(
            "verify_audit_integrity", {}, "admin-1", "admin",
        )

        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["valid"] is True
        assert payload["total_checked"] == 42
        assert payload["first_invalid_id"] is None
        assert payload["range"] == {"start_id": None, "end_id": None}

        # SQL should call pb_verify_audit_chain with NULL bounds
        args = mock_pool.fetchrow.call_args[0]
        assert "pb_verify_audit_chain" in args[0]
        assert args[1] is None  # start_id
        assert args[2] is None  # end_id

    async def test_admin_with_range(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _verify_row(valid=True, total_checked=10)

        result = await _dispatch(
            "verify_audit_integrity",
            {"start_id": 100, "end_id": 200},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["range"] == {"start_id": 100, "end_id": 200}
        args = mock_pool.fetchrow.call_args[0]
        assert args[1] == 100
        assert args[2] == 200

    async def test_admin_detects_invalid_chain(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _verify_row(
            valid=False, first_invalid_id=42, total_checked=41,
        )

        result = await _dispatch(
            "verify_audit_integrity", {}, "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["valid"] is False
        assert payload["first_invalid_id"] == 42

    async def test_non_admin_denied(self, _patch_globals):
        _, mock_pool = _patch_globals
        mock_pool.fetchrow.return_value = _verify_row()

        result = await _dispatch(
            "verify_audit_integrity", {}, "analyst-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "admin" in payload["error"].lower()
        # DB must not be touched for denied requests
        mock_pool.fetchrow.assert_not_called()

    async def test_viewer_denied(self, _patch_globals):
        _, mock_pool = _patch_globals
        result = await _dispatch(
            "verify_audit_integrity", {}, "viewer-1", "viewer",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload
        mock_pool.fetchrow.assert_not_called()


def _audit_row(row_id: int, agent_id: str = "a1", action: str = "search",
               policy_reason=None):
    return mock_row({
        "id":              row_id,
        "agent_id":        agent_id,
        "agent_role":      "analyst",
        "resource_type":   "dataset",
        "resource_id":     f"d{row_id}",
        "action":          action,
        "policy_result":   "allow",
        "policy_reason":   policy_reason,
        "contains_pii":    False,
        "purpose":         None,
        "legal_basis":     None,
        "data_category":   None,
        "fields_redacted": None,
        "created_at":      datetime(2026, 4, 8, 12, 0, row_id, tzinfo=timezone.utc),
        "prev_hash":       "00" * 32,
        "entry_hash":      f"{row_id:064x}",
    })


class TestExportAuditLog:
    def _opa_response(self, cfg=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "result": cfg if cfg is not None else {
                "retention_days": 365,
                "advisory_lock_id": 847291,
                "export_max_rows": 100000,
                "export_default_rows": 10000,
                "export_formats": ["json", "csv"],
            }
        }
        return resp

    async def test_json_export_happy_path(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.get.return_value = self._opa_response()
        mock_pool.fetch.return_value = [_audit_row(1), _audit_row(2)]

        result = await _dispatch(
            "export_audit_log", {}, "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["format"] == "json"
        assert payload["count"] == 2
        assert len(payload["entries"]) == 2
        assert payload["entries"][0]["id"] == 1
        assert payload["entries"][0]["created_at"].startswith("2026-04-08T")
        # Hash fields exposed as hex
        assert payload["entries"][0]["entry_hash"] == "0" * 63 + "1"

    async def test_csv_export(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.get.return_value = self._opa_response()
        mock_pool.fetch.return_value = [_audit_row(1), _audit_row(2)]

        result = await _dispatch(
            "export_audit_log", {"format": "csv"}, "admin-1", "admin",
        )
        body = result[0].text
        lines = body.strip().splitlines()
        assert lines[0].startswith("id,agent_id,agent_role")
        assert len(lines) == 3  # header + 2 rows

    async def test_limit_capped_by_max_rows(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.get.return_value = self._opa_response(cfg={
            "export_max_rows": 50,
            "export_default_rows": 10,
            "export_formats": ["json"],
        })
        mock_pool.fetch.return_value = [_audit_row(i) for i in range(50)]

        result = await _dispatch(
            "export_audit_log", {"limit": 999999}, "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["limit"] == 50
        # Generated SQL contains the capped limit
        sql = mock_pool.fetch.call_args[0][0]
        assert "LIMIT 50" in sql

    async def test_default_limit_when_missing(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.get.return_value = self._opa_response(cfg={
            "export_max_rows": 100000,
            "export_default_rows": 250,
            "export_formats": ["json"],
        })
        mock_pool.fetch.return_value = []

        await _dispatch("export_audit_log", {}, "admin-1", "admin")
        sql = mock_pool.fetch.call_args[0][0]
        assert "LIMIT 250" in sql

    async def test_filters_applied(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.get.return_value = self._opa_response()
        mock_pool.fetch.return_value = []

        await _dispatch(
            "export_audit_log",
            {
                "since":    "2026-04-01T00:00:00Z",
                "until":    "2026-04-08T00:00:00Z",
                "agent_id": "agent-42",
                "action":   "search",
            },
            "admin-1", "admin",
        )
        call = mock_pool.fetch.call_args
        sql = call[0][0]
        params = list(call[0][1:])
        assert "created_at >=" in sql
        assert "created_at <" in sql
        assert "agent_id =" in sql
        assert "action =" in sql
        assert "2026-04-01T00:00:00Z" in params
        assert "2026-04-08T00:00:00Z" in params
        assert "agent-42" in params
        assert "search" in params

    async def test_unknown_format_rejected(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.get.return_value = self._opa_response(cfg={
            "export_max_rows": 100,
            "export_default_rows": 10,
            "export_formats": ["json"],  # csv not allowed
        })

        result = await _dispatch(
            "export_audit_log", {"format": "csv"}, "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload
        mock_pool.fetch.assert_not_called()

    async def test_non_admin_denied(self, _patch_globals):
        mock_http, mock_pool = _patch_globals

        result = await _dispatch(
            "export_audit_log", {}, "analyst-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "admin" in payload["error"].lower()
        mock_http.get.assert_not_called()
        mock_pool.fetch.assert_not_called()

    async def test_csv_handles_special_characters(self, _patch_globals):
        """RFC 4180 quoting: newlines and quotes in policy_reason must
        not break CSV parseability."""
        mock_http, mock_pool = _patch_globals
        mock_http.get.return_value = self._opa_response()
        nasty = _audit_row(1, policy_reason='foo\nbar"baz,qux')
        mock_pool.fetch.return_value = [nasty]

        result = await _dispatch(
            "export_audit_log", {"format": "csv"}, "admin-1", "admin",
        )
        body = result[0].text

        # Reparse the CSV — if escaping is correct, csv.reader recovers
        # the original value byte-for-byte.
        import csv, io
        reader = csv.DictReader(io.StringIO(body))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["policy_reason"] == 'foo\nbar"baz,qux'

    async def test_opa_failure_falls_back_to_defaults(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.get.side_effect = Exception("OPA down")
        mock_pool.fetch.return_value = []

        result = await _dispatch(
            "export_audit_log", {}, "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        # Should succeed with hardcoded defaults
        assert payload["format"] == "json"
        assert payload["count"] == 0


# ── Live Postgres tests for the SQL functions ──────────────
# These are gated by PG_INTEGRATION=1 because they need a live
# pb-postgres with migration 014 applied. They cover the parts of
# B-40 that cannot be exercised through Python mocks alone:
# genesis hash, re-chain after checkpoint, append-only enforcement.

import os as _os

_PG_INTEGRATION = _os.environ.get("PG_INTEGRATION") == "1"


@pytest.mark.skipif(not _PG_INTEGRATION,
                    reason="set PG_INTEGRATION=1 to run live PG tests")
class TestHashChainLive:
    @pytest.fixture
    async def live_pool(self):
        import asyncpg
        url = _os.environ.get(
            "POSTGRES_URL",
            "postgresql://pb_admin:pb_admin@localhost:5432/powerbrain",
        )
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        # Clean state — test class assumes an empty agent_access_log AND
        # an empty audit_archive (otherwise the trigger would use a stale
        # checkpoint as the genesis prev_hash anchor).
        await pool.execute("DELETE FROM agent_access_log")
        await pool.execute("DELETE FROM audit_archive")
        yield pool
        await pool.execute("DELETE FROM agent_access_log")
        await pool.execute("DELETE FROM audit_archive")
        await pool.close()

    async def _insert(self, pool, n: int, action: str = "search",
                      created_at_offset_days: int = 0):
        for i in range(n):
            await pool.execute(
                "INSERT INTO agent_access_log "
                "(agent_id, agent_role, resource_type, resource_id, "
                " action, policy_result, created_at) "
                "VALUES ($1, 'analyst', 'dataset', $2, $3, 'allow', "
                "        now() - make_interval(days => $4))",
                f"pytest-chain-{action}", f"d{i}", action, created_at_offset_days,
            )

    async def test_genesis_chain_starts_from_zero(self, live_pool):
        await self._insert(live_pool, 1, action="genesis")
        row = await live_pool.fetchrow(
            "SELECT prev_hash, entry_hash FROM agent_access_log "
            "WHERE agent_id = 'pytest-chain-genesis' ORDER BY id DESC LIMIT 1"
        )
        assert row["prev_hash"] == b"\x00" * 32
        assert row["entry_hash"] is not None
        assert len(row["entry_hash"]) == 32

    async def test_chain_links_subsequent_inserts(self, live_pool):
        await self._insert(live_pool, 3, action="link")
        rows = await live_pool.fetch(
            "SELECT id, prev_hash, entry_hash FROM agent_access_log "
            "WHERE agent_id = 'pytest-chain-link' ORDER BY id ASC"
        )
        assert len(rows) == 3
        for i in range(1, 3):
            assert rows[i]["prev_hash"] == rows[i - 1]["entry_hash"]

    async def test_verify_full_chain_after_inserts(self, live_pool):
        await self._insert(live_pool, 3, action="verify")
        v = await live_pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert v["valid"] is True

    async def test_tail_verifier_caps_scan(self, live_pool):
        await self._insert(live_pool, 5, action="tail")
        v = await live_pool.fetchrow(
            "SELECT * FROM pb_verify_audit_chain_tail($1)", 2,
        )
        assert v["valid"] is True
        assert v["total_checked"] <= 2

    async def test_update_is_blocked(self, live_pool):
        await self._insert(live_pool, 1, action="immut")
        with pytest.raises(Exception):
            await live_pool.execute(
                "UPDATE agent_access_log SET policy_result = 'tampered' "
                "WHERE agent_id = 'pytest-chain-immut'"
            )

    async def test_checkpoint_and_rechain(self, live_pool):
        """When ALL rows are pruned, the next INSERT must anchor to
        the archive checkpoint hash, not to the genesis."""
        await self._insert(live_pool, 3, action="rechain", created_at_offset_days=10)

        cp = await live_pool.fetchrow(
            "SELECT * FROM pb_audit_checkpoint_and_prune(1)"
        )
        assert cp["chain_valid"] is True
        assert cp["deleted_count"] == 3

        # Table is empty → next INSERT chains from archive checkpoint
        remaining = await live_pool.fetchval(
            "SELECT COUNT(*) FROM agent_access_log"
        )
        assert remaining == 0

        await self._insert(live_pool, 1, action="rechain", created_at_offset_days=0)
        row = await live_pool.fetchrow(
            "SELECT prev_hash FROM agent_access_log "
            "WHERE agent_id = 'pytest-chain-rechain' ORDER BY id DESC LIMIT 1"
        )
        archive_tail = await live_pool.fetchval(
            "SELECT last_verified_hash FROM audit_archive "
            "ORDER BY archived_at DESC LIMIT 1"
        )
        assert row["prev_hash"] == archive_tail

        v = await live_pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert v["valid"] is True

    # ── Issue #94: verifier must detect inconsistent seed ─────────
    async def test_verify_detects_inconsistent_seed(self, live_pool):
        """An empty agent_access_log with audit_tail.last_entry_hash
        out of sync with the archive's last_verified_hash is a guaranteed
        chain break on the next insert. The verifier must surface that
        proactively (#94)."""
        # Setup: archive carries a non-genesis hash, tail is genesis.
        archive_hash = b"\xab" * 32
        await live_pool.execute(
            "INSERT INTO audit_archive ("
            "    archived_at, last_entry_id, last_verified_hash, "
            "    row_count, chain_valid, first_invalid_id, retention_cutoff"
            ") VALUES (now(), 0, $1, 0, true, NULL, now())",
            archive_hash,
        )
        await live_pool.execute(
            "UPDATE audit_tail SET last_entry_hash = $1, last_entry_id = 0 "
            "WHERE id = 1",
            b"\x00" * 32,
        )

        v = await live_pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert v["valid"] is False
        assert v["first_invalid_id"] == 1
        assert v["total_checked"] == 0
        assert v["last_valid_hash"] == archive_hash

    async def test_verify_consistent_empty_chain(self, live_pool):
        """Empty log + tail and archive both at genesis is a valid
        post-genesis-reset state. Verifier must return valid=true."""
        await live_pool.execute(
            "UPDATE audit_tail SET last_entry_hash = $1, last_entry_id = 0 "
            "WHERE id = 1",
            b"\x00" * 32,
        )
        v = await live_pool.fetchrow("SELECT * FROM pb_verify_audit_chain()")
        assert v["valid"] is True
        assert v["total_checked"] == 0
        assert v["last_valid_hash"] == b"\x00" * 32

    async def test_verify_seed_check_skipped_for_range_query(self, live_pool):
        """When the caller asks about a specific id range (p_start_id > 1),
        the tail-mismatch check must NOT trigger — they're not asking
        about chain-head consistency."""
        # Empty log, mismatched seeds (would trigger if scope were head)
        archive_hash = b"\xcd" * 32
        await live_pool.execute(
            "INSERT INTO audit_archive ("
            "    archived_at, last_entry_id, last_verified_hash, "
            "    row_count, chain_valid, first_invalid_id, retention_cutoff"
            ") VALUES (now(), 0, $1, 0, true, NULL, now())",
            archive_hash,
        )
        await live_pool.execute(
            "UPDATE audit_tail SET last_entry_hash = $1 WHERE id = 1",
            b"\x00" * 32,
        )

        v = await live_pool.fetchrow(
            "SELECT * FROM pb_verify_audit_chain($1, $2)", 100, 200,
        )
        # Range scope, empty result → still considered valid
        assert v["valid"] is True
        assert v["total_checked"] == 0
