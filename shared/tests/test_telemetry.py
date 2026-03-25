# shared/tests/test_telemetry.py
"""Tests for shared telemetry module."""
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
