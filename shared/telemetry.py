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


# ── OTel Initialization ──────────────────────────────────────

_tracer_cache: dict[str, Any] = {}


def init_telemetry(service_name: str) -> Any:
    """Initialize OTel tracing. Returns a Tracer or None if disabled.

    Safe to call multiple times — returns cached tracer for the same service.
    Reads OTEL_ENABLED at call time (not module-load time) for testability.
    """
    if os.getenv("OTEL_ENABLED", "true").lower() != "true":
        return None

    if service_name in _tracer_cache:
        return _tracer_cache[service_name]

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT))
        )
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer(service_name)
        _tracer_cache[service_name] = tracer
        log.info("OTel tracing enabled → %s (service: %s)", OTLP_ENDPOINT, service_name)
        return tracer
    except ImportError:
        log.warning("opentelemetry packages not installed, tracing disabled")
        return None


def setup_auto_instrumentation(app: Any = None) -> None:
    """Set up auto-instrumentation for FastAPI and httpx.

    Call after init_telemetry(). Pass the FastAPI app instance.
    """
    if not OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        log.info("httpx auto-instrumentation enabled (traceparent propagation)")
    except ImportError:
        log.debug("opentelemetry-instrumentation-httpx not installed")

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
            log.info("FastAPI auto-instrumentation enabled")
        except ImportError:
            log.debug("opentelemetry-instrumentation-fastapi not installed")


# ── trace_operation ──────────────────────────────────────────

@contextmanager
def trace_operation(
    tracer: Any,
    name: str,
    service: str,
    **attributes: Any,
) -> Generator[None, None, None]:
    """Create an OTel span and record a PipelineStep in the current RequestTelemetry.

    Works with or without an active telemetry context.
    Works with or without a tracer (no-op if tracer is None).
    """
    t0 = time.perf_counter()
    status = "ok"

    # Start OTel span if tracer available
    span_ctx = None
    if tracer:
        span_ctx = tracer.start_as_current_span(
            name, attributes={k: str(v) for k, v in attributes.items()}
        )
        span_ctx.__enter__()

    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        duration_ms = (time.perf_counter() - t0) * 1000

        if span_ctx:
            span_ctx.__exit__(None, None, None)

        # Record step in telemetry context if active
        rt = _current_telemetry.get()
        if rt is not None:
            rt.add_step(PipelineStep(
                name=name,
                service=service,
                duration_ms=round(duration_ms, 2),
                status=status,
                metadata=attributes,
            ))
