# Metrics & Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose structured JSON metrics and per-request OTel telemetry from all 4 Powerbrain services, consumable by the demo-UI without PromQL.

**Architecture:** Shared `telemetry.py` module provides OTel initialization, per-request `RequestTelemetry` context, and `MetricsAggregator` (reads from Prometheus registry). Each service gets `GET /metrics/json` endpoint and per-request `_telemetry` in responses. W3C `traceparent` propagated across all inter-service calls via auto-instrumented httpx.

**Tech Stack:** Python 3.12, opentelemetry-api/sdk/exporter-otlp, opentelemetry-instrumentation-fastapi, opentelemetry-instrumentation-httpx, prometheus_client, cachetools, contextvars

---

## File Inventory

### Create
- `shared/telemetry.py` — OTel init, RequestTelemetry dataclass, trace_operation context manager, MetricsAggregator
- `shared/tests/test_telemetry.py` — Unit tests for telemetry module

### Modify
- `shared/__init__.py` — (no changes needed, stays empty)
- `mcp-server/requirements.txt` — Add `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-httpx`
- `mcp-server/server.py` — Replace OTel code (lines 155-178), wire telemetry into search pipeline, add `/metrics/json` endpoint
- `pb-proxy/requirements.txt` — Add all OTel dependencies
- `pb-proxy/proxy.py` — Wire telemetry into chat endpoint, add `/metrics/json` endpoint
- `pb-proxy/agent_loop.py` — Add telemetry spans for tool dispatch and LLM calls
- `reranker/requirements.txt` — Add OTel dependencies
- `reranker/service.py` — Wire telemetry, add `/metrics/json` endpoint, trace propagation
- `ingestion/requirements.txt` — Add OTel + prometheus-client dependencies
- `ingestion/ingestion_api.py` — Add Prometheus metrics, wire telemetry, add `/metrics/json` endpoint
- `docker-compose.yml` — Flip `OTEL_ENABLED` default to `true`, add `TELEMETRY_IN_RESPONSE`, add OTel env vars to proxy/reranker/ingestion
- `.env.example` — Document new env vars
- `monitoring/prometheus.yml` — Verify ingestion scrape target (already configured but service had no metrics)
- `CLAUDE.md` — Update docs

---

### Task 1: Shared Telemetry Module — Core Dataclasses and OTel Init

**Files:**
- Create: `shared/telemetry.py`
- Create: `shared/tests/test_telemetry.py`

- [ ] **Step 1: Write failing tests for PipelineStep and RequestTelemetry**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd shared && python -m pytest tests/test_telemetry.py -v`
Expected: ImportError — `shared.telemetry` does not exist yet

- [ ] **Step 3: Implement PipelineStep and RequestTelemetry dataclasses**

```python
# shared/telemetry.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd shared && python -m pytest tests/test_telemetry.py -v`
Expected: All 4 tests pass

- [ ] **Step 5: Commit**

```bash
git add shared/telemetry.py shared/tests/test_telemetry.py
git commit -m "feat: add telemetry module — PipelineStep, RequestTelemetry dataclasses"
```

---

### Task 2: Shared Telemetry Module — OTel Init and trace_operation

**Files:**
- Modify: `shared/telemetry.py`
- Modify: `shared/tests/test_telemetry.py`

- [ ] **Step 1: Write failing tests for init_telemetry and trace_operation**

Add to `shared/tests/test_telemetry.py`:

```python
from unittest.mock import patch, MagicMock


class TestInitTelemetry:
    @patch.dict("os.environ", {"OTEL_ENABLED": "false"})
    def test_returns_none_when_disabled(self):
        # Re-import to pick up env change
        from shared.telemetry import init_telemetry
        tracer = init_telemetry("test-service")
        assert tracer is None

    @patch.dict("os.environ", {"OTEL_ENABLED": "true"})
    def test_returns_tracer_when_enabled(self):
        from shared.telemetry import init_telemetry
        tracer = init_telemetry("test-service")
        assert tracer is not None


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd shared && python -m pytest tests/test_telemetry.py -v`
Expected: FAIL — `init_telemetry` and `trace_operation` not defined

- [ ] **Step 3: Implement init_telemetry and trace_operation**

Append to `shared/telemetry.py`:

```python
# ── OTel Initialization ──────────────────────────────────────

_tracer_cache: dict[str, Any] = {}


def init_telemetry(service_name: str) -> Any:
    """Initialize OTel tracing. Returns a Tracer or None if disabled.

    Safe to call multiple times — returns cached tracer for the same service.
    """
    if not OTEL_ENABLED:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd shared && python -m pytest tests/test_telemetry.py -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add shared/telemetry.py shared/tests/test_telemetry.py
git commit -m "feat: add OTel init and trace_operation to telemetry module"
```

---

### Task 3: Shared Telemetry Module — MetricsAggregator

**Files:**
- Modify: `shared/telemetry.py`
- Modify: `shared/tests/test_telemetry.py`

- [ ] **Step 1: Write failing tests for MetricsAggregator**

Add to `shared/tests/test_telemetry.py`:

```python
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry


class TestMetricsAggregator:
    def test_snapshot_counter(self):
        from shared.telemetry import MetricsAggregator
        registry = CollectorRegistry()
        c = Counter("test_requests_total", "Test", ["status"], registry=registry)
        c.labels(status="ok").inc(10)
        c.labels(status="error").inc(2)

        agg = MetricsAggregator("test-service", registry=registry)
        snap = agg.snapshot()
        assert snap["service"] == "test-service"
        assert "uptime_seconds" in snap

    def test_snapshot_histogram_percentiles(self):
        from shared.telemetry import MetricsAggregator
        registry = CollectorRegistry()
        h = Histogram(
            "test_duration_seconds", "Test", ["tool"],
            buckets=[0.1, 0.5, 1.0, 5.0],
            registry=registry,
        )
        for _ in range(100):
            h.labels(tool="search").observe(0.3)

        agg = MetricsAggregator("test-service", registry=registry)
        snap = agg.snapshot()
        # Should have raw metrics available
        assert snap["service"] == "test-service"

    def test_snapshot_empty_registry(self):
        from shared.telemetry import MetricsAggregator
        registry = CollectorRegistry()
        agg = MetricsAggregator("test-service", registry=registry)
        snap = agg.snapshot()
        assert snap["service"] == "test-service"
        assert snap["raw_metrics"] == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd shared && python -m pytest tests/test_telemetry.py::TestMetricsAggregator -v`
Expected: FAIL — `MetricsAggregator` not defined

- [ ] **Step 3: Implement MetricsAggregator**

Append to `shared/telemetry.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd shared && python -m pytest tests/test_telemetry.py -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add shared/telemetry.py shared/tests/test_telemetry.py
git commit -m "feat: add MetricsAggregator to telemetry module"
```

---

### Task 4: MCP-Server — Replace OTel Code and Wire Telemetry

**Files:**
- Modify: `mcp-server/requirements.txt` — Add `opentelemetry-instrumentation-fastapi>=0.41b0` and `opentelemetry-instrumentation-httpx>=0.48b0`
- Modify: `mcp-server/server.py` — Replace OTel setup (lines 155-178), wire `trace_operation` into search pipeline, add `_telemetry` to responses

- [ ] **Step 1: Add new OTel instrumentation dependencies**

In `mcp-server/requirements.txt`, add after the existing `opentelemetry-instrumentation-httpx` line:

```
opentelemetry-instrumentation-fastapi>=0.41b0
```

(The `opentelemetry-instrumentation-httpx>=0.48b0` is already present.)

- [ ] **Step 2: Replace OTel setup in server.py (lines 87-88 and 155-178)**

Replace the `OTEL_ENABLED`/`OTLP_ENDPOINT` env var reads (lines 87-88) and the OTel setup block (lines 155-178) with:

```python
# At line ~80, after embedding_cache import, add:
from shared.telemetry import (
    init_telemetry, setup_auto_instrumentation, trace_operation,
    request_telemetry_context, get_current_telemetry, TELEMETRY_IN_RESPONSE,
)

# Replace lines 87-88:
# Remove: OTEL_ENABLED = os.getenv(...)
# Remove: OTLP_ENDPOINT = os.getenv(...)

# Replace lines 155-178 (the entire OTel block) with:
tracer = init_telemetry("pb-mcp-server")
```

- [ ] **Step 3: Wire trace_operation into search_knowledge (lines 1095-1215)**

Replace the pipeline calls with traced versions. Key changes in `_dispatch()`:

```python
# search_knowledge section:

# Embedding (line 1095):
with trace_operation(tracer, "embedding", "mcp-server",
                     model=EMBEDDING_MODEL, text_length=len(query)):
    vector = await embed_text(query)

# Vector search (lines 1100-1104):
with trace_operation(tracer, "vector_search", "mcp-server",
                     collection=collection, top_k=oversample_k):
    results = await qdrant.query_points(...)

# OPA policy filter (lines 1106-1108):
with trace_operation(tracer, "opa_policy", "mcp-server",
                     role=agent_role):
    allowed_hits = await filter_by_policy(...)

# Reranking (line 1119):
with trace_operation(tracer, "reranking", "mcp-server",
                     input_count=len(filtered), top_n=top_k):
    reranked = await rerank_results(query, filtered, top_n=top_k)

# Summarization (lines 1196/1204):
with trace_operation(tracer, "summarization", "mcp-server",
                     model=LLM_MODEL, policy=summary_policy):
    summary = await summarize_text(...)
```

- [ ] **Step 4: Add telemetry context to tool call handler (lines 1064-1079)**

Wrap the tool dispatch in a `request_telemetry_context`:

```python
# Replace lines 1064-1079:
t_start = time.perf_counter()
status = "ok"

# Generate trace_id from OTel or fallback
import uuid as _uuid
trace_id = _uuid.uuid4().hex[:16]
if tracer:
    from opentelemetry import trace as _trace
    span = _trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.trace_id:
        trace_id = format(ctx.trace_id, '032x')

with request_telemetry_context(trace_id) as req_telemetry:
    with trace_operation(tracer, f"mcp.{name}", "mcp-server", tool=name):
        try:
            result = await _dispatch(name, arguments, agent_id, agent_role)
        except Exception as e:
            log.error(f"Tool {name} fehlgeschlagen: {e}", exc_info=True)
            status = "error"
            result = [TextContent(type="text", text=json.dumps({"error": str(e)}))]

elapsed = time.perf_counter() - t_start
mcp_requests_total.labels(tool=name, status=status).inc()
mcp_request_duration.labels(tool=name).observe(elapsed)
```

- [ ] **Step 5: Inject _telemetry into JSON responses**

At the end of `search_knowledge` (before the return at line 1214), and similarly for `get_code_context` and other search tools:

```python
# Before return:
if TELEMETRY_IN_RESPONSE:
    rt = get_current_telemetry()
    if rt is not None:
        response_data["_telemetry"] = rt.to_dict()
```

- [ ] **Step 6: Run existing MCP server tests to verify no regressions**

Run: `cd mcp-server && python -m pytest tests/ -v`
Expected: All existing tests pass

- [ ] **Step 7: Commit**

```bash
git add mcp-server/requirements.txt mcp-server/server.py
git commit -m "feat: wire OTel telemetry into MCP server search pipeline"
```

---

### Task 5: MCP-Server — /metrics/json Endpoint

**Files:**
- Modify: `mcp-server/server.py` — Add `/metrics/json` route

- [ ] **Step 1: Add MetricsAggregator and /metrics/json route**

In `server.py`, after the existing metrics definitions (around line 153), add:

```python
from shared.telemetry import MetricsAggregator
_metrics_agg = MetricsAggregator("mcp-server")
```

In the Starlette routes (around line 1771), add a new route:

```python
async def metrics_json(request):
    """Structured JSON metrics for demo-UI consumption."""
    snap = _metrics_agg.snapshot()

    # Build structured response from known metrics
    response = {
        "service": "mcp-server",
        "uptime_seconds": snap["uptime_seconds"],
        "requests": {
            "total": sum(
                v for k, v in snap["raw_metrics"].items()
                if k.startswith("pb_mcp_requests_total")
            ),
            "by_tool": {},
            "by_status": {},
            "rate_limited": sum(
                v for k, v in snap["raw_metrics"].items()
                if k.startswith("pb_rate_limit_rejected_total")
            ),
        },
        "latency": {},
        "policy": {
            "decisions_total": {},
            "cache_hit_ratio": _opa_cache_hit_ratio(),
        },
        "search": {"results_avg": {}},
        "reranker": {
            "fallbacks_total": snap["raw_metrics"].get(
                "pb_mcp_rerank_fallback_total", 0
            ),
        },
        "embedding_cache": embedding_cache.stats(),
        "feedback": {
            "avg_rating_24h": snap["raw_metrics"].get("pb_feedback_avg_rating", 0),
        },
    }

    # Aggregate by_tool and by_status from labeled counters
    for key, val in snap["raw_metrics"].items():
        if key.startswith("pb_mcp_requests_total{"):
            labels = _parse_prom_labels(key)
            tool = labels.get("tool", "unknown")
            status = labels.get("status", "unknown")
            response["requests"]["by_tool"][tool] = (
                response["requests"]["by_tool"].get(tool, 0) + val
            )
            response["requests"]["by_status"][status] = (
                response["requests"]["by_status"].get(status, 0) + val
            )
        elif key.startswith("pb_mcp_policy_decisions_total{"):
            labels = _parse_prom_labels(key)
            result = labels.get("result", "unknown")
            response["policy"]["decisions_total"][result] = val

    # Latency percentiles per tool
    for tool in response["requests"]["by_tool"]:
        response["latency"][tool] = _metrics_agg.histogram_percentiles(
            "pb_mcp_request_duration_seconds", {"tool": tool}
        )

    return JSONResponse(response)


def _parse_prom_labels(key: str) -> dict[str, str]:
    """Parse 'metric{k1=v1,k2=v2}' into dict."""
    if "{" not in key:
        return {}
    label_str = key.split("{", 1)[1].rstrip("}")
    labels = {}
    for part in label_str.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            labels[k] = v
    return labels


def _opa_cache_hit_ratio() -> float:
    """Calculate OPA cache hit ratio from cache stats."""
    with _opa_cache_lock:
        total = getattr(_opa_cache, '_hits', 0) + getattr(_opa_cache, '_misses', 0)
        if total == 0:
            return 0.0
        return round(getattr(_opa_cache, '_hits', 0) / total, 3)
```

Add to Starlette routes (line ~1771):

```python
Route("/metrics/json", endpoint=metrics_json),
```

- [ ] **Step 2: Run existing tests**

Run: `cd mcp-server && python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add mcp-server/server.py
git commit -m "feat: add /metrics/json endpoint to MCP server"
```

---

### Task 6: Proxy — Wire OTel Telemetry

**Files:**
- Modify: `pb-proxy/requirements.txt` — Add OTel dependencies
- Modify: `pb-proxy/proxy.py` — Wire telemetry, add `/metrics/json`
- Modify: `pb-proxy/agent_loop.py` — Add spans for LLM calls and tool dispatch

- [ ] **Step 1: Add OTel dependencies to proxy requirements**

Append to `pb-proxy/requirements.txt`:

```
opentelemetry-api>=1.20
opentelemetry-sdk>=1.20
opentelemetry-exporter-otlp-proto-grpc>=1.20
opentelemetry-instrumentation-fastapi>=0.41b0
opentelemetry-instrumentation-httpx>=0.48b0
```

- [ ] **Step 2: Wire telemetry into proxy.py**

Add imports after line 41:

```python
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.telemetry import (
    init_telemetry, setup_auto_instrumentation, trace_operation,
    request_telemetry_context, get_current_telemetry,
    MetricsAggregator, TELEMETRY_IN_RESPONSE,
)
```

After the `app = FastAPI(...)` creation (line ~208), add:

```python
_proxy_tracer = init_telemetry("pb-proxy")
setup_auto_instrumentation(app)
_proxy_metrics = MetricsAggregator("pb-proxy")
```

In `chat_completions()` (line 286), wrap the main logic:

```python
# After start_time = time.monotonic() (line 287):
import uuid as _uuid
trace_id = _uuid.uuid4().hex[:16]

# Wrap the entire handler body in:
with request_telemetry_context(trace_id) as req_telemetry:
    # ... auth section wrapped:
    with trace_operation(_proxy_tracer, "auth", "pb-proxy",
                         agent_id=agent_id, agent_role=agent_role):
        # existing auth code

    # ... PII section wrapped:
    with trace_operation(_proxy_tracer, "pii_pseudonymize", "pb-proxy"):
        # existing PII pseudonymization code

    # ... agent loop wrapped:
    with trace_operation(_proxy_tracer, "agent_loop", "pb-proxy",
                         model=request.model, max_iterations=max_iterations):
        # existing agent loop call
```

- [ ] **Step 3: Add _telemetry to proxy JSON response (around line 443-472)**

Before building the response (line 442):

```python
# Inject telemetry into response
if TELEMETRY_IN_RESPONSE:
    rt = get_current_telemetry()
    if rt is not None:
        rt.finish()
        response_data["_telemetry"] = rt.to_dict()
```

- [ ] **Step 4: Add /metrics/json endpoint to proxy**

Add before the SSE helpers section (around line 475):

```python
@app.get("/metrics/json")
async def metrics_json():
    snap = _proxy_metrics.snapshot()
    response = {
        "service": "pb-proxy",
        "uptime_seconds": snap["uptime_seconds"],
        "requests": {
            "total": sum(v for k, v in snap["raw_metrics"].items()
                        if k.startswith("pbproxy_requests_total")),
            "by_model": {},
            "by_status": {},
        },
        "latency": {"by_model": {}},
        "agent_loop": {
            "iterations_avg": 0,
            "max_iterations_reached": 0,
        },
        "tool_calls": {"total": 0, "by_tool": {}},
        "pii": {"entities_pseudonymized": {}, "scan_failures": {}},
    }
    # Parse labeled counters from raw_metrics
    for key, val in snap["raw_metrics"].items():
        if key.startswith("pbproxy_requests_total{"):
            labels = _parse_prom_labels(key)
            model = labels.get("model", "unknown")
            status = labels.get("status", "unknown")
            response["requests"]["by_model"][model] = (
                response["requests"]["by_model"].get(model, 0) + val
            )
            response["requests"]["by_status"][status] = (
                response["requests"]["by_status"].get(status, 0) + val
            )
        elif key.startswith("pbproxy_tool_calls_total{"):
            labels = _parse_prom_labels(key)
            tool = labels.get("tool_name", "unknown")
            response["tool_calls"]["by_tool"][tool] = val
            response["tool_calls"]["total"] += val
        elif key.startswith("pbproxy_pii_entities_pseudonymized_total{"):
            labels = _parse_prom_labels(key)
            entity = labels.get("entity_type", "unknown")
            response["pii"]["entities_pseudonymized"][entity] = val
        elif key.startswith("pbproxy_pii_scan_failures_total{"):
            labels = _parse_prom_labels(key)
            mode = labels.get("fail_mode", "unknown")
            response["pii"]["scan_failures"][mode] = val

    # Latency percentiles per model
    for model in response["requests"]["by_model"]:
        response["latency"]["by_model"][model] = _proxy_metrics.histogram_percentiles(
            "pbproxy_request_latency_seconds", {"model": model}
        )

    return JSONResponse(content=response)


def _parse_prom_labels(key: str) -> dict[str, str]:
    if "{" not in key:
        return {}
    label_str = key.split("{", 1)[1].rstrip("}")
    labels = {}
    for part in label_str.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            labels[k] = v
    return labels
```

- [ ] **Step 5: Wire telemetry into agent_loop.py**

In `agent_loop.py`, add import:

```python
from shared.telemetry import trace_operation, get_current_telemetry
```

Wrap the LLM call (line 68) and tool dispatch (in the tool execution section after line 80):

```python
# LLM call (line 68):
with trace_operation(None, "llm_call", "pb-proxy",
                     model=model, iteration=iteration):
    response = await self._acompletion(...)

# Tool dispatch (in the tool execution loop):
with trace_operation(None, "tool_dispatch", "pb-proxy",
                     tool_name=tool_name):
    tool_result = await self._execute_tool(...)
```

- [ ] **Step 6: Run proxy tests**

Run: `cd pb-proxy && python -m pytest tests/ -v`
Expected: All existing tests pass

- [ ] **Step 7: Commit**

```bash
git add pb-proxy/requirements.txt pb-proxy/proxy.py pb-proxy/agent_loop.py
git commit -m "feat: wire OTel telemetry and /metrics/json into AI proxy"
```

---

### Task 7: Reranker — Wire OTel and /metrics/json

**Files:**
- Modify: `reranker/requirements.txt` — Add OTel dependencies
- Modify: `reranker/service.py` — Wire telemetry, add `/metrics/json`

- [ ] **Step 1: Add OTel dependencies**

Append to `reranker/requirements.txt`:

```
opentelemetry-api>=1.20
opentelemetry-sdk>=1.20
opentelemetry-exporter-otlp-proto-grpc>=1.20
opentelemetry-instrumentation-fastapi>=0.41b0
```

- [ ] **Step 2: Wire telemetry into reranker**

Add imports at top of `service.py`:

```python
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.telemetry import (
    init_telemetry, setup_auto_instrumentation, trace_operation,
    MetricsAggregator,
)
```

After `app = FastAPI(...)` (line 74):

```python
_reranker_tracer = init_telemetry("pb-reranker")
setup_auto_instrumentation(app)
_reranker_metrics = MetricsAggregator("reranker")
```

Wrap the rerank endpoint's core logic with a span (in the POST handler):

```python
with trace_operation(_reranker_tracer, "rerank", "reranker",
                     input_count=len(req.documents), top_n=req.top_n):
    # existing scoring logic
```

- [ ] **Step 3: Add /metrics/json endpoint**

```python
@app.get("/metrics/json")
async def metrics_json():
    snap = _reranker_metrics.snapshot()
    response = {
        "service": "reranker",
        "uptime_seconds": snap["uptime_seconds"],
        "requests": {"total": 0, "ok": 0, "error": 0},
        "latency": _reranker_metrics.histogram_percentiles("pb_reranker_duration_seconds"),
        "batch_size": {
            "avg": 0,
            "p95": _reranker_metrics.histogram_percentiles(
                "pb_reranker_batch_size"
            ).get("p95_ms", 0) / 1000,  # batch_size is not in ms
        },
        "model_load_seconds": 0,
    }
    for key, val in snap["raw_metrics"].items():
        if "pb_reranker_requests_total" in key:
            if "ok" in key:
                response["requests"]["ok"] = val
            elif "error" in key:
                response["requests"]["error"] = val
            response["requests"]["total"] = (
                response["requests"]["ok"] + response["requests"]["error"]
            )
    return JSONResponse(content=response)
```

- [ ] **Step 4: Run reranker tests**

Run: `cd reranker && python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add reranker/requirements.txt reranker/service.py
git commit -m "feat: wire OTel telemetry and /metrics/json into reranker"
```

---

### Task 8: Ingestion — Add Prometheus Metrics, OTel, and /metrics/json

**Files:**
- Modify: `ingestion/requirements.txt` — Add OTel + prometheus-client
- Modify: `ingestion/ingestion_api.py` — Add metrics, telemetry, `/metrics/json`

- [ ] **Step 1: Add dependencies**

Append to `ingestion/requirements.txt`:

```
prometheus-client>=0.21
opentelemetry-api>=1.20
opentelemetry-sdk>=1.20
opentelemetry-exporter-otlp-proto-grpc>=1.20
opentelemetry-instrumentation-fastapi>=0.41b0
opentelemetry-instrumentation-httpx>=0.48b0
```

- [ ] **Step 2: Add Prometheus metrics to ingestion_api.py**

After the logging setup (line 75), add:

```python
from prometheus_client import Counter, Histogram, make_asgi_app as prom_make_asgi_app

pb_ingestion_requests = Counter(
    "pb_ingestion_requests_total", "Ingestion requests", ["endpoint", "status"],
)
pb_ingestion_duration = Histogram(
    "pb_ingestion_duration_seconds", "Ingestion request duration", ["endpoint"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)
pb_ingestion_chunks = Counter(
    "pb_ingestion_chunks_total", "Total chunks ingested", ["collection"],
)
pb_ingestion_pii_entities = Counter(
    "pb_ingestion_pii_entities_total", "PII entities found", ["entity_type", "action"],
)
pb_ingestion_embedding_batch = Histogram(
    "pb_ingestion_embedding_batch_size", "Embedding batch size",
    buckets=[1, 5, 10, 20, 50, 100],
)
```

Mount Prometheus metrics app:

```python
metrics_app = prom_make_asgi_app()
app.mount("/metrics", metrics_app)
```

- [ ] **Step 3: Wire telemetry imports and init**

```python
from shared.telemetry import (
    init_telemetry, setup_auto_instrumentation, trace_operation,
    request_telemetry_context, get_current_telemetry,
    MetricsAggregator, TELEMETRY_IN_RESPONSE,
)

_ingestion_tracer = init_telemetry("pb-ingestion")
setup_auto_instrumentation(app)
_ingestion_metrics = MetricsAggregator("ingestion")
```

- [ ] **Step 4: Add /metrics/json endpoint**

```python
@app.get("/metrics/json")
async def metrics_json():
    snap = _ingestion_metrics.snapshot()
    response = {
        "service": "ingestion",
        "uptime_seconds": snap["uptime_seconds"],
        "requests": {"total": 0, "ok": 0, "error": 0},
        "chunks": {"total": 0, "avg_per_request": 0},
        "pii": {"scans_total": 0, "entities_found": {}},
        "embedding": {
            "batch_total": 0,
            "cache_hit_ratio": embedding_cache.stats().get("hits", 0) / max(
                embedding_cache.stats().get("hits", 0) +
                embedding_cache.stats().get("misses", 0), 1,
            ),
        },
    }
    for key, val in snap["raw_metrics"].items():
        if "pb_ingestion_requests_total" in key:
            if "ok" in key:
                response["requests"]["ok"] += val
            elif "error" in key:
                response["requests"]["error"] += val
        elif "pb_ingestion_chunks_total" in key:
            response["chunks"]["total"] += val
        elif "pb_ingestion_pii_entities_total" in key:
            # Parse entity_type label
            if "entity_type=" in key:
                et = key.split("entity_type=")[1].split(",")[0].split("}")[0]
                response["pii"]["entities_found"][et] = (
                    response["pii"]["entities_found"].get(et, 0) + val
                )
    response["requests"]["total"] = response["requests"]["ok"] + response["requests"]["error"]
    return JSONResponse(content=response)
```

- [ ] **Step 5: Instrument key ingestion endpoints with metrics**

Wrap the `/ingest` and `/scan` handlers with timing:

```python
# In each endpoint handler:
t0 = time.perf_counter()
try:
    # ... existing logic ...
    pb_ingestion_requests.labels(endpoint="ingest", status="ok").inc()
except Exception:
    pb_ingestion_requests.labels(endpoint="ingest", status="error").inc()
    raise
finally:
    pb_ingestion_duration.labels(endpoint="ingest").observe(time.perf_counter() - t0)
```

- [ ] **Step 6: Run ingestion tests**

Run: `cd ingestion && python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add ingestion/requirements.txt ingestion/ingestion_api.py
git commit -m "feat: add Prometheus metrics, OTel, and /metrics/json to ingestion"
```

---

### Task 9: Docker Compose and Configuration Updates

**Files:**
- Modify: `docker-compose.yml` — Flip OTEL_ENABLED default, add env vars
- Modify: `.env.example` — Document new env vars
- Modify: `CLAUDE.md` — Update documentation

- [ ] **Step 1: Update docker-compose.yml**

Change MCP-Server environment (line 214):
```yaml
OTEL_ENABLED:    ${OTEL_ENABLED:-true}
```
(was `false`)

Add to MCP-Server environment:
```yaml
TELEMETRY_IN_RESPONSE: ${TELEMETRY_IN_RESPONSE:-true}
```

Add to pb-proxy environment (after line ~408):
```yaml
OTEL_ENABLED:            ${OTEL_ENABLED:-true}
OTLP_ENDPOINT:           http://tempo:4317
TELEMETRY_IN_RESPONSE:   ${TELEMETRY_IN_RESPONSE:-true}
```

Add to reranker environment:
```yaml
OTEL_ENABLED:    ${OTEL_ENABLED:-true}
OTLP_ENDPOINT:   http://tempo:4317
```

Add to ingestion environment:
```yaml
OTEL_ENABLED:    ${OTEL_ENABLED:-true}
OTLP_ENDPOINT:   http://tempo:4317
TELEMETRY_IN_RESPONSE: ${TELEMETRY_IN_RESPONSE:-true}
```

- [ ] **Step 2: Update .env.example**

Add:
```bash
# ── Telemetry ──────────────────────────────────────────────
OTEL_ENABLED=true                    # Enable OpenTelemetry tracing (all services)
TELEMETRY_IN_RESPONSE=true           # Include _telemetry in JSON responses
```

- [ ] **Step 3: Update CLAUDE.md**

Add to the "Completed Features" section:
```
19. ✅ **Metrics & Telemetry** — OTel tracing across all services, per-request `_telemetry` in responses, `GET /metrics/json` JSON endpoints, W3C traceparent propagation
```

Update the "Components and Ports" table to note the `/metrics/json` endpoints.

Update the "Key Concepts" section with a "Telemetry" subsection describing the per-request telemetry and JSON metrics.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example CLAUDE.md
git commit -m "feat: enable OTel by default, add telemetry env vars, update docs"
```

---

### Task 10: Cleanup — Remove Generated Context Files

**Files:**
- Delete: `IMPLEMENTATION_CONTEXT.md`, `IMPLEMENTATION_QUICK_REF.md`, `CODE_CONTEXT_INDEX.md` (generated by explore agent)

- [ ] **Step 1: Remove generated files**

```bash
rm -f IMPLEMENTATION_CONTEXT.md IMPLEMENTATION_QUICK_REF.md CODE_CONTEXT_INDEX.md
```

- [ ] **Step 2: Final test run across all services**

```bash
cd shared && python -m pytest tests/ -v
cd ../mcp-server && python -m pytest tests/ -v
cd ../pb-proxy && python -m pytest tests/ -v
cd ../reranker && python -m pytest tests/ -v
cd ../ingestion && python -m pytest tests/ -v
```

- [ ] **Step 3: Commit cleanup**

```bash
git add -A
git commit -m "chore: cleanup generated context files"
```
