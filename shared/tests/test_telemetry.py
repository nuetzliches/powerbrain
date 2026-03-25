# shared/tests/test_telemetry.py
"""Tests for shared telemetry module."""
import time
from unittest.mock import patch, MagicMock

import pytest


class TestPipelineStep:
    def test_to_dict_basic(self):
        from shared.telemetry import PipelineStep
        step = PipelineStep(
            name="embedding", service="mcp-server",
            duration_ms=45.2, status="ok", metadata={"cache_hit": True},
        )
        d = step.to_dict()
        assert d["name"] == "embedding"
        assert d["service"] == "mcp-server"
        assert d["ms"] == 45.2
        assert d["status"] == "ok"
        assert d["cache_hit"] is True

    def test_to_dict_empty_metadata(self):
        from shared.telemetry import PipelineStep
        step = PipelineStep(
            name="test", service="test", duration_ms=10.0,
            status="ok", metadata={},
        )
        d = step.to_dict()
        assert "name" in d
        assert "ms" in d
        # No extra keys from empty metadata
        assert set(d.keys()) == {"name", "service", "ms", "status"}


class TestRequestTelemetry:
    def test_add_step_and_to_dict(self):
        from shared.telemetry import RequestTelemetry, PipelineStep
        rt = RequestTelemetry(trace_id="abc123")
        rt.add_step(PipelineStep(
            name="embedding", service="mcp-server",
            duration_ms=45.0, status="ok", metadata={},
        ))
        rt.add_step(PipelineStep(
            name="qdrant", service="mcp-server",
            duration_ms=120.0, status="ok", metadata={"results": 50},
        ))
        rt.finish()
        d = rt.to_dict()
        assert d["trace_id"] == "abc123"
        assert d["total_ms"] >= 0  # calculated from finish - start
        assert len(d["steps"]) == 2
        assert d["steps"][0]["name"] == "embedding"
        assert d["steps"][1]["results"] == 50

    def test_to_dict_without_finish_uses_zero(self):
        from shared.telemetry import RequestTelemetry
        rt = RequestTelemetry(trace_id="test")
        d = rt.to_dict()
        assert d["total_ms"] == 0
        assert d["steps"] == []


class TestInitTelemetry:
    @patch.dict("os.environ", {"OTEL_ENABLED": "false"})
    def test_returns_none_when_disabled(self):
        from shared.telemetry import init_telemetry
        tracer = init_telemetry("test-service")
        assert tracer is None

    @patch.dict("os.environ", {"OTEL_ENABLED": "true"})
    def test_returns_tracer_when_enabled(self):
        from shared.telemetry import init_telemetry
        try:
            import opentelemetry  # noqa: F401
            has_otel = True
        except ImportError:
            has_otel = False
        # Clear cache so init_telemetry re-initializes
        from shared.telemetry import _tracer_cache
        _tracer_cache.pop("test-otel-service", None)
        tracer = init_telemetry("test-otel-service")
        if has_otel:
            assert tracer is not None
        else:
            # Graceful degradation: returns None when OTel not installed
            assert tracer is None


class TestTraceOperation:
    def test_records_step_in_telemetry_context(self):
        from shared.telemetry import (
            trace_operation, request_telemetry_context,
        )
        with request_telemetry_context("test-trace") as rt:
            with trace_operation(None, "embedding", "mcp-server", cache_hit=True):
                time.sleep(0.01)  # Simulate work
        assert len(rt.steps) == 1
        assert rt.steps[0].name == "embedding"
        assert rt.steps[0].metadata["cache_hit"] is True
        assert rt.steps[0].duration_ms >= 10  # At least 10ms

    def test_records_error_status_on_exception(self):
        from shared.telemetry import (
            trace_operation, request_telemetry_context,
        )
        with request_telemetry_context("test-trace") as rt:
            try:
                with trace_operation(None, "failing_step", "test"):
                    raise ValueError("test error")
            except ValueError:
                pass
        assert rt.steps[0].status == "error"

    def test_no_op_without_telemetry_context(self):
        from shared.telemetry import trace_operation
        # Should not raise even without a request telemetry context
        with trace_operation(None, "orphan", "test"):
            pass
