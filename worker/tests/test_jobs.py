"""Tests for pb-worker jobs and scheduler registration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from worker import scheduler as scheduler_mod
from worker.jobs import (
    accuracy_metrics,
    audit_integrity_status,
    audit_retention,
    pending_review_timeout,
    gdpr_retention,
)


def _ctx(**overrides):
    """Synthetic WorkerContext stand-in for unit tests."""
    pool = AsyncMock()
    base = SimpleNamespace(
        pg_pool=pool,
        http_client=AsyncMock(),
        opa_url="http://opa:8181",
        qdrant_url="http://qdrant:6333",
        audit_retention_days=365,
        audit_status_tail_rows=1000,
        pending_review_grace_minutes=0,
        extra={},
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _make_response(json_body):
    from unittest.mock import MagicMock
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = json_body
    return r


# ── audit_retention ────────────────────────────────────────

class TestAuditRetention:
    async def test_happy_path_valid_chain(self):
        ctx = _ctx()
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "checkpoint_id":    1,
            "last_entry_id":    100,
            "row_count":        50,
            "deleted_count":    50,
            "chain_valid":      True,
            "first_invalid_id": None,
        }[k]
        ctx.pg_pool.fetchrow.return_value = row

        summary = await audit_retention.run(ctx)
        assert summary["chain_valid"] is True
        assert summary["deleted_count"] == 50
        assert summary["retention_days"] == 365
        ctx.pg_pool.fetchrow.assert_called_once()

    async def test_invalid_chain_no_delete(self):
        ctx = _ctx()
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "checkpoint_id":    1,
            "last_entry_id":    100,
            "row_count":        50,
            "deleted_count":    0,
            "chain_valid":      False,
            "first_invalid_id": 42,
        }[k]
        ctx.pg_pool.fetchrow.return_value = row

        summary = await audit_retention.run(ctx)
        assert summary["chain_valid"] is False
        assert summary["deleted_count"] == 0
        assert summary["first_invalid_id"] == 42

    async def test_zero_retention_skipped(self):
        ctx = _ctx(audit_retention_days=0)
        summary = await audit_retention.run(ctx)
        assert summary.get("skipped") is True
        ctx.pg_pool.fetchrow.assert_not_called()

    async def test_db_exception_propagates(self):
        ctx = _ctx()
        ctx.pg_pool.fetchrow.side_effect = RuntimeError("connection lost")
        with pytest.raises(RuntimeError, match="connection lost"):
            await audit_retention.run(ctx)


# ── audit_integrity_status ─────────────────────────────────

class TestAuditIntegrityStatus:
    def _verify_row(self, valid=True, total=10, first_invalid=None,
                    last_hash=b"\xab" * 32):
        row = MagicMock()
        row.__getitem__ = lambda s, k: {
            "valid":            valid,
            "total_checked":    total,
            "first_invalid_id": first_invalid,
            "last_valid_hash":  last_hash,
        }[k]
        return row

    async def test_happy_path_upserts_cache(self):
        ctx = _ctx()
        ctx.pg_pool.fetchrow.return_value = self._verify_row(
            valid=True, total=42,
        )
        summary = await audit_integrity_status.run(ctx)
        assert summary["valid"] is True
        assert summary["total_checked"] == 42
        assert summary["tail_rows"] == 1000
        # Verifier called with tail_rows
        ctx.pg_pool.fetchrow.assert_called_once()
        verify_args = ctx.pg_pool.fetchrow.call_args[0]
        assert "pb_verify_audit_chain_tail" in verify_args[0]
        assert verify_args[1] == 1000
        # UPSERT executed
        ctx.pg_pool.execute.assert_called_once()
        upsert_sql = ctx.pg_pool.execute.call_args[0][0]
        assert "audit_integrity_status" in upsert_sql
        assert "ON CONFLICT (id) DO UPDATE" in upsert_sql

    async def test_invalid_chain_logged(self, caplog):
        import logging
        ctx = _ctx()
        ctx.pg_pool.fetchrow.return_value = self._verify_row(
            valid=False, total=10, first_invalid=5,
        )
        with caplog.at_level(logging.ERROR,
                             logger="pb-worker.audit_integrity_status"):
            summary = await audit_integrity_status.run(ctx)
        assert summary["valid"] is False
        assert summary["first_invalid_id"] == 5
        assert any("audit chain invalid" in r.message
                   for r in caplog.records)

    async def test_custom_tail_rows(self):
        ctx = _ctx(audit_status_tail_rows=500)
        ctx.pg_pool.fetchrow.return_value = self._verify_row(total=500)
        summary = await audit_integrity_status.run(ctx)
        assert summary["tail_rows"] == 500
        verify_args = ctx.pg_pool.fetchrow.call_args[0]
        assert verify_args[1] == 500

    async def test_db_failure_persists_error_and_raises(self):
        ctx = _ctx()
        ctx.pg_pool.fetchrow.side_effect = RuntimeError("db unreachable")
        # The error-path execute() must succeed
        ctx.pg_pool.execute.return_value = None
        with pytest.raises(RuntimeError, match="db unreachable"):
            await audit_integrity_status.run(ctx)
        # Error UPSERT call after the verify failure
        ctx.pg_pool.execute.assert_called_once()
        sql = ctx.pg_pool.execute.call_args[0][0]
        assert "audit_integrity_status" in sql
        assert "error" in sql.lower()
        assert "db unreachable" in ctx.pg_pool.execute.call_args[0][1]

    async def test_error_persistence_failure_swallowed(self):
        """If the error UPSERT itself fails, the original exception
        must still propagate; the secondary failure must not mask it."""
        ctx = _ctx()
        ctx.pg_pool.fetchrow.side_effect = RuntimeError("primary failure")
        ctx.pg_pool.execute.side_effect = RuntimeError("secondary failure")
        with pytest.raises(RuntimeError, match="primary failure"):
            await audit_integrity_status.run(ctx)


# ── pending_review_timeout ─────────────────────────────────

class TestPendingReviewTimeout:
    async def test_no_expired(self):
        ctx = _ctx()
        ctx.pg_pool.fetch.return_value = []
        summary = await pending_review_timeout.run(ctx)
        assert summary["expired_count"] == 0
        assert summary["agents_affected"] == []

    async def test_expires_rows(self):
        ctx = _ctx()
        rows = [
            {"id": "r1", "agent_id": "a1", "tool": "search_knowledge",
             "classification": "confidential", "expires_at": None},
            {"id": "r2", "agent_id": "a2", "tool": "query_data",
             "classification": "restricted", "expires_at": None},
        ]
        ctx.pg_pool.fetch.return_value = rows
        summary = await pending_review_timeout.run(ctx)
        assert summary["expired_count"] == 2
        assert sorted(summary["agents_affected"]) == ["a1", "a2"]
        assert "search_knowledge" in summary["tools"]

    async def test_grace_minutes_passed_to_query(self):
        ctx = _ctx(pending_review_grace_minutes=30)
        ctx.pg_pool.fetch.return_value = []
        summary = await pending_review_timeout.run(ctx)
        assert summary["grace_minutes"] == 30
        # Verify grace value was passed as $1 to the query
        call_args = ctx.pg_pool.fetch.call_args
        assert call_args[0][1] == 30


# ── accuracy_metrics ───────────────────────────────────────

class TestAccuracyMetrics:
    def _make_qdrant_response(self, n: int, dim: int = 4):
        return _make_response({
            "result": {
                "points": [
                    {"id": i, "vector": [float(i)] * dim}
                    for i in range(n)
                ]
            }
        })

    def _opa_response(self, cfg=None):
        return _make_response({
            "result": cfg if cfg is not None else {
                "thresholds": {"default": 0.5, "pb_general": 0.08,
                               "pb_code": 0.12, "pb_rules": 0.05},
                "reference_sample_size": 10,
                "fresh_sample_size":     10,
            }
        })

    @pytest.fixture(autouse=True)
    def _reset_view_cache(self):
        """The accuracy_metrics module caches v_feedback_windowed
        existence; reset it between tests so each test sees a fresh
        check."""
        accuracy_metrics._VIEW_EXISTS = None
        yield
        accuracy_metrics._VIEW_EXISTS = None

    async def test_no_view_returns_empty_windows(self):
        ctx = _ctx()
        ctx.pg_pool.fetchval.return_value = False  # view absent
        ctx.pg_pool.fetchrow.return_value = None  # no baseline yet

        # OPA call goes through ctx.http_client.get now
        ctx.http_client.get.return_value = self._opa_response()
        ctx.http_client.post.return_value = self._make_qdrant_response(0)

        summary = await accuracy_metrics.run(ctx)
        assert summary["windows"] == []

    async def test_view_and_baseline_seed(self):
        ctx = _ctx()
        ctx.pg_pool.fetchval.return_value = True
        ctx.pg_pool.fetch.return_value = [
            {"window_label": "1h", "collection": "pb_general",
             "sample_count": 10, "avg_rating": 4.2,
             "empty_result_rate": 0.1, "avg_rerank_score": 0.7},
        ]
        ctx.pg_pool.fetchrow.return_value = None

        ctx.http_client.get.return_value = self._opa_response()
        ctx.http_client.post.return_value = self._make_qdrant_response(5)

        summary = await accuracy_metrics.run(ctx)
        assert len(summary["windows"]) == 1
        # 3 collections × seed insert each
        assert ctx.pg_pool.execute.call_count >= 3

    async def test_empty_window_skipped(self):
        """Windows with sample_count == 0 must be skipped, not exported
        as 0.0 (which would trip the QualityDrift alert)."""
        ctx = _ctx()
        ctx.pg_pool.fetchval.return_value = True
        ctx.pg_pool.fetch.return_value = [
            {"window_label": "1h", "collection": "_all_",
             "sample_count": 0, "avg_rating": None,
             "empty_result_rate": None, "avg_rerank_score": None},
        ]
        ctx.pg_pool.fetchrow.return_value = None
        ctx.http_client.get.return_value = self._opa_response()
        ctx.http_client.post.return_value = self._make_qdrant_response(0)

        summary = await accuracy_metrics.run(ctx)
        assert summary["windows"] == []

    async def test_drift_detection_flags_collection(self):
        ctx = _ctx()
        ctx.pg_pool.fetchval.return_value = True
        ctx.pg_pool.fetch.return_value = []

        from unittest.mock import MagicMock as MM
        baseline_row = MM()
        baseline_row.__getitem__ = lambda s, k: {
            "id": 1, "sample_count": 100, "embedding_dim": 4,
            "centroid": [1.0, 0.0, 0.0, 0.0],
        }[k]
        ctx.pg_pool.fetchrow.return_value = baseline_row

        ctx.http_client.get.return_value = self._opa_response(cfg={
            "thresholds": {"default": 0.05},
            "reference_sample_size": 10,
            "fresh_sample_size":     10,
        })

        orthogonal_response = _make_response({
            "result": {
                "points": [
                    {"id": i, "vector": [0.0, 1.0, 0.0, 0.0]}
                    for i in range(5)
                ]
            }
        })
        ctx.http_client.post.return_value = orthogonal_response

        summary = await accuracy_metrics.run(ctx)
        assert len(summary["drift"]) == 3  # all 3 canonical collections
        assert all(d["drifted"] for d in summary["drift"])
        assert sorted(summary["drifted"]) == ["pb_code", "pb_general", "pb_rules"]
        assert summary.get("skipped") == []

    async def test_zero_fresh_vectors_marks_skipped(self):
        """When Qdrant returns no fresh vectors, the collection must
        appear in summary['skipped'] (not silently in 'drift')."""
        ctx = _ctx()
        ctx.pg_pool.fetchval.return_value = True
        ctx.pg_pool.fetch.return_value = []

        from unittest.mock import MagicMock as MM
        baseline_row = MM()
        baseline_row.__getitem__ = lambda s, k: {
            "id": 1, "sample_count": 100, "embedding_dim": 4,
            "centroid": [1.0, 0.0, 0.0, 0.0],
        }[k]
        ctx.pg_pool.fetchrow.return_value = baseline_row

        ctx.http_client.get.return_value = self._opa_response()
        ctx.http_client.post.return_value = self._make_qdrant_response(0)

        summary = await accuracy_metrics.run(ctx)
        assert summary["drift"] == []
        assert sorted(summary["skipped"]) == ["pb_code", "pb_general", "pb_rules"]

    async def test_load_drift_config_opa_failure_uses_fallback(self):
        """When OPA is unreachable, _load_drift_config returns defaults."""
        ctx = _ctx()
        ctx.http_client.get.side_effect = httpx.ConnectError("connection refused")

        from shared.drift_check import DEFAULT_THRESHOLDS
        cfg = await accuracy_metrics._load_drift_config(ctx)
        assert cfg["thresholds"] == dict(DEFAULT_THRESHOLDS)
        assert cfg["reference_sample_size"] == 200
        assert cfg["fresh_sample_size"] == 200

    async def test_load_drift_config_opa_timeout_uses_fallback(self):
        """Timeout from OPA also returns fallback defaults."""
        ctx = _ctx()
        ctx.http_client.get.side_effect = httpx.TimeoutException("timed out")

        cfg = await accuracy_metrics._load_drift_config(ctx)
        assert "thresholds" in cfg
        assert cfg["reference_sample_size"] == 200

    async def test_load_drift_config_empty_result_uses_fallback(self):
        """OPA returns 200 but empty result → fallback."""
        ctx = _ctx()
        ctx.http_client.get.return_value = _make_response({"result": {}})

        cfg = await accuracy_metrics._load_drift_config(ctx)
        assert cfg["reference_sample_size"] == 200

    async def test_sample_collection_vectors_qdrant_error(self):
        """Qdrant failure returns empty list instead of raising."""
        ctx = _ctx()
        ctx.http_client.post.side_effect = httpx.ConnectError("qdrant down")

        vectors = await accuracy_metrics._sample_collection_vectors(
            ctx, "pb_general", 10,
        )
        assert vectors == []

    async def test_sample_collection_vectors_multi_named(self):
        """Points with multi-named vectors pick first key alphabetically."""
        ctx = _ctx()
        resp = _make_response({
            "result": {
                "points": [
                    {"id": 0, "vector": {"zebra": [9.0, 9.0], "alpha": [1.0, 2.0]}},
                    {"id": 1, "vector": {"zebra": [8.0, 8.0], "alpha": [3.0, 4.0]}},
                ]
            }
        })
        ctx.http_client.post.return_value = resp

        vectors = await accuracy_metrics._sample_collection_vectors(
            ctx, "pb_general", 10,
        )
        # Should pick "alpha" (sorted first)
        assert vectors == [[1.0, 2.0], [3.0, 4.0]]

    async def test_ensure_baseline_exists_no_insert(self):
        """When baseline already exists, no INSERT is executed."""
        ctx = _ctx()
        existing = MagicMock()
        existing.__getitem__ = lambda s, k: {
            "id": 42, "sample_count": 200,
            "embedding_dim": 768, "centroid": [0.1] * 768,
        }[k]
        ctx.pg_pool.fetchrow.return_value = existing

        result = await accuracy_metrics._ensure_reference_baseline(
            ctx, "pb_general", 200,
        )
        assert result["id"] == 42
        ctx.pg_pool.execute.assert_not_called()

    async def test_ensure_baseline_no_vectors_returns_none(self):
        """Empty collection → baseline returns None, no INSERT."""
        ctx = _ctx()
        ctx.pg_pool.fetchrow.return_value = None  # no existing baseline
        ctx.http_client.post.return_value = self._make_qdrant_response(0)

        result = await accuracy_metrics._ensure_reference_baseline(
            ctx, "pb_general", 200,
        )
        assert result is None
        ctx.pg_pool.execute.assert_not_called()


# ── gdpr_retention ─────────────────────────────────────────

class TestGdprRetention:
    def _fake_proc(self, returncode=0, stdout=b"", stderr=b""):
        async def _communicate():
            return (stdout, stderr)
        proc = MagicMock()
        proc.communicate = _communicate
        proc.returncode = returncode
        return proc

    async def test_subprocess_returncode_propagated(self, monkeypatch):
        ctx = _ctx()
        proc = self._fake_proc(returncode=0, stdout=b"deleted: 0")

        import asyncio
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=proc))

        result = await gdpr_retention.run(ctx)
        assert result["exit_code"] == 0
        assert "deleted: 0" in result["stdout"]

    async def test_nonzero_exit_code(self, monkeypatch):
        ctx = _ctx()
        proc = self._fake_proc(returncode=1, stderr=b"error: table locked")

        import asyncio
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=proc))

        result = await gdpr_retention.run(ctx)
        assert result["exit_code"] == 1
        assert "table locked" in result["stderr"]

    async def test_stderr_captured(self, monkeypatch):
        ctx = _ctx()
        proc = self._fake_proc(returncode=0, stdout=b"ok",
                               stderr=b"WARN: low disk space")

        import asyncio
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=proc))

        result = await gdpr_retention.run(ctx)
        assert "low disk space" in result["stderr"]

    async def test_file_not_found_returns_skipped(self, monkeypatch):
        ctx = _ctx()

        import asyncio
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(side_effect=FileNotFoundError("python")))

        result = await gdpr_retention.run(ctx)
        assert result["skipped"] is True

    async def test_generic_exception_returns_error(self, monkeypatch):
        ctx = _ctx()

        import asyncio
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(side_effect=OSError("permission denied")))

        result = await gdpr_retention.run(ctx)
        assert "error" in result
        assert "permission denied" in result["error"]


# ── scheduler registration ────────────────────────────────

class TestSchedulerRegistration:
    def test_job_specs_complete(self):
        ids = {spec["id"] for spec in scheduler_mod.JOB_SPECS}
        assert ids == {
            "accuracy_metrics_refresh",
            "pending_review_timeout",
            "gdpr_retention_cleanup",
            "audit_retention_cleanup",
            "repo_sync",
            "audit_integrity_status_refresh",
        }

    def test_register_jobs_attaches_all(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        sched = AsyncIOScheduler()
        ctx = _ctx()
        scheduler_mod.register_jobs(sched, ctx)
        registered = {j.id for j in sched.get_jobs()}
        assert registered == {
            "accuracy_metrics_refresh",
            "pending_review_timeout",
            "gdpr_retention_cleanup",
            "audit_retention_cleanup",
            "repo_sync",
            "audit_integrity_status_refresh",
        }

    async def test_run_with_logging_swallows_exceptions(self):
        ctx = _ctx()
        async def _failing(_ctx):
            raise RuntimeError("boom")
        # Must not propagate
        await scheduler_mod._run_with_logging("failing-job", _failing, ctx)

    async def test_run_with_logging_completes_on_success(self):
        ctx = _ctx()
        async def _ok(_ctx):
            return {"status": "done"}
        # Must not raise
        await scheduler_mod._run_with_logging("ok-job", _ok, ctx)

    def test_job_trigger_types(self):
        from apscheduler.triggers.interval import IntervalTrigger
        from apscheduler.triggers.cron import CronTrigger

        trigger_map = {s["id"]: type(s["trigger"]) for s in scheduler_mod.JOB_SPECS}
        assert trigger_map["accuracy_metrics_refresh"] is IntervalTrigger
        assert trigger_map["pending_review_timeout"] is IntervalTrigger
        assert trigger_map["gdpr_retention_cleanup"] is CronTrigger
        assert trigger_map["audit_retention_cleanup"] is CronTrigger
        assert trigger_map["audit_integrity_status_refresh"] is IntervalTrigger
