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


# ── Metrics Aggregator ───────────────────────────────────────

import math

_service_start_time: float = time.time()


def _histogram_percentile(buckets: list[tuple[float, float]], count: int, q: float) -> float:
    """Approximate percentile from histogram bucket counts using linear interpolation."""
    if count == 0:
        return 0.0
    target = q * count
    prev_bound = 0.0
    prev_count = 0.0
    for bound, cum_count in buckets:
        if cum_count >= target:
            # Linear interpolation within this bucket
            if cum_count == prev_count:
                return bound
            fraction = (target - prev_count) / (cum_count - prev_count)
            return prev_bound + fraction * (bound - prev_bound)
        prev_bound = bound
        prev_count = cum_count
    # Above all buckets
    return buckets[-1][0] if buckets else 0.0


class MetricsAggregator:
    """Reads from Prometheus registry and returns structured JSON snapshot."""

    def __init__(self, service_name: str, registry: Any = None):
        self._service_name = service_name
        self._registry = registry

    def _get_registry(self) -> Any:
        if self._registry is not None:
            return self._registry
        from prometheus_client import REGISTRY
        return REGISTRY

    def snapshot(self) -> dict[str, Any]:
        """Return a structured dict of all metrics in the registry."""
        registry = self._get_registry()
        raw: dict[str, Any] = {}

        for metric in registry.collect():
            if metric.name.startswith("python_") or metric.name.startswith("process_"):
                continue  # Skip default process metrics
            for sample in metric.samples:
                name = sample.name
                labels = sample.labels
                value = sample.value
                if math.isnan(value) or math.isinf(value):
                    continue

                key = name
                if labels:
                    label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
                    key = f"{name}{{{label_str}}}"
                raw[key] = value

        return {
            "service": self._service_name,
            "uptime_seconds": round(time.time() - _service_start_time, 1),
            "raw_metrics": raw,
        }

    def histogram_percentiles(
        self, metric_name: str, label_filter: dict[str, str] | None = None,
    ) -> dict[str, float]:
        """Calculate p50/p95/p99 from a histogram metric."""
        registry = self._get_registry()
        buckets: list[tuple[float, float]] = []
        count = 0

        for metric in registry.collect():
            if metric.name != metric_name:
                continue
            for sample in metric.samples:
                if label_filter:
                    if not all(sample.labels.get(k) == v for k, v in label_filter.items()):
                        continue
                if sample.name.endswith("_bucket"):
                    le = sample.labels.get("le", "")
                    if le == "+Inf":
                        continue
                    buckets.append((float(le), sample.value))
                elif sample.name.endswith("_count"):
                    count = int(sample.value)

        buckets.sort(key=lambda x: x[0])
        return {
            "p50_ms": round(_histogram_percentile(buckets, count, 0.50) * 1000, 1),
            "p95_ms": round(_histogram_percentile(buckets, count, 0.95) * 1000, 1),
            "p99_ms": round(_histogram_percentile(buckets, count, 0.99) * 1000, 1),
        }
