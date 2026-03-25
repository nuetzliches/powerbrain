"""
Shared telemetry module for Powerbrain services.

Provides:
- OTel initialization (TracerProvider + OTLP exporter)
- Per-request telemetry context (RequestTelemetry + PipelineStep)
- trace_operation context manager (creates OTel span + records PipelineStep)
- MetricsAggregator (reads Prometheus registry, returns structured JSON)

Configuration:
  OTEL_ENABLED           (default: true)
  OTLP_ENDPOINT          (default: http://tempo:4317)
  TELEMETRY_IN_RESPONSE  (default: true)
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

log = logging.getLogger("pb-telemetry")

# ── Configuration ────────────────────────────────────────────

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "true").lower() == "true"
OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://tempo:4317")
TELEMETRY_IN_RESPONSE = os.getenv("TELEMETRY_IN_RESPONSE", "true").lower() == "true"


# ── Per-Request Telemetry ────────────────────────────────────

@dataclass
class PipelineStep:
    """A single step in the processing pipeline."""
    name: str
    service: str
    duration_ms: float
    status: str  # "ok", "error", "skipped"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "service": self.service,
            "ms": self.duration_ms,
            "status": self.status,
        }
        # Flatten metadata into the dict for easy consumption
        for k, v in self.metadata.items():
            d[k] = v
        return d


@dataclass
class RequestTelemetry:
    """Accumulates telemetry for a single request."""
    trace_id: str
    steps: list[PipelineStep] = field(default_factory=list)
    _start_time: float = field(default_factory=time.perf_counter)
    _total_ms: float = 0.0

    def add_step(self, step: PipelineStep) -> None:
        self.steps.append(step)

    def finish(self) -> None:
        self._total_ms = (time.perf_counter() - self._start_time) * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "total_ms": round(self._total_ms, 1),
            "steps": [s.to_dict() for s in self.steps],
        }


# ── Context Variable ─────────────────────────────────────────

_current_telemetry: contextvars.ContextVar[RequestTelemetry | None] = (
    contextvars.ContextVar("_current_telemetry", default=None)
)


def get_current_telemetry() -> RequestTelemetry | None:
    """Get the telemetry context for the current request (if any)."""
    return _current_telemetry.get()


@contextmanager
def request_telemetry_context(trace_id: str) -> Generator[RequestTelemetry, None, None]:
    """Create and manage a per-request telemetry context."""
    rt = RequestTelemetry(trace_id=trace_id)
    token = _current_telemetry.set(rt)
    try:
        yield rt
    finally:
        rt.finish()
        _current_telemetry.reset(token)
