"""Tests for pb-worker jobs and scheduler registration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker import scheduler as scheduler_mod
from worker.jobs import (
    accuracy_metrics,
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


# ── gdpr_retention ─────────────────────────────────────────

class TestGdprRetention:
    async def test_subprocess_returncode_propagated(self, monkeypatch):
        ctx = _ctx()

        async def _fake_communicate():
            return (b"deleted: 0", b"")

        fake_proc = MagicMock()
        fake_proc.communicate = _fake_communicate
        fake_proc.returncode = 0

        async def _fake_create(*args, **kwargs):
            return fake_proc

        import asyncio
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)

        result = await gdpr_retention.run(ctx)
        assert result["exit_code"] == 0
        assert "deleted: 0" in result["stdout"]


# ── scheduler registration ────────────────────────────────

class TestSchedulerRegistration:
    def test_job_specs_complete(self):
        ids = {spec["id"] for spec in scheduler_mod.JOB_SPECS}
        assert ids == {
            "accuracy_metrics_refresh",
            "pending_review_timeout",
            "gdpr_retention_cleanup",
            "audit_retention_cleanup",
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
        }

    async def test_run_with_logging_swallows_exceptions(self):
        ctx = _ctx()
        async def _failing(_ctx):
            raise RuntimeError("boom")
        # Must not propagate
        await scheduler_mod._run_with_logging("failing-job", _failing, ctx)
