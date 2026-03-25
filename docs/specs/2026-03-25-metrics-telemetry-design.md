# Metrics & Telemetry Design

**Date:** 2026-03-25
**Status:** Approved
**Goal:** Expose structured, demo-consumable metrics from all Powerbrain services — per-request telemetry and aggregated JSON endpoints — powered by OTel trace propagation. Prometheus stays as collector/alerting; Grafana stays optional (ops-only).

## Context

Powerbrain has 17 Prometheus metrics across MCP-Server (7), Reranker (4), and Proxy (6). Ingestion has zero metrics. All metrics are Prometheus-format only — no JSON endpoints, no request correlation, no per-request timing breakdown.

The [powerbrain-demo UI spec](../../powerbrain-demo/docs/superpowers/specs/2026-03-25-demo-ui-design.md) requires:
- **Pipeline View:** Per-request timeline showing each processing step with duration and status
- **Dashboard:** Aggregated metrics (request volume, latency percentiles, policy decisions, PII entities)
- **Request correlation:** Events assigned to the correct request via trace ID

Currently, the demo-backend would need to run PromQL queries against Prometheus and parse generic metric names. This is fragile and an unnecessary indirection.

## Constraints

- Prometheus stays as collector and alerting backend — no removal
- Grafana stays available as optional ops tool — but the demo does not depend on it
- No new infrastructure services (no Redis, no extra DBs)
- OTel Tempo already runs in the stack (port 4317) — currently receives no traces
- `shared/` module pattern already exists (`llm_provider.py`, `embedding_cache.py`)
- All configuration via environment variables with backward-compatible defaults

## Design

### 1. Shared Telemetry Module (`shared/telemetry.py`)

Central module imported by all services. Three responsibilities:

#### OTel Initialization

```python
def init_telemetry(service_name: str) -> Tracer:
    """Configure TracerProvider, BatchSpanProcessor, OTLP exporter."""
```

- Configures `TracerProvider` + `BatchSpanProcessor` + OTLP gRPC exporter (Tempo)
- Replaces existing OTel code in MCP-Server (`server.py` lines 155-178)
- Returns a `Tracer` instance the service uses for manual spans
- Auto-instrumentation for FastAPI and httpx registered here

Environment variables:

| Env var | Default | Description |
|---------|---------|-------------|
| `OTEL_ENABLED` | `true` | Enable/disable tracing (flipped from current `false` default) |
| `OTLP_ENDPOINT` | `http://tempo:4317` | OTLP gRPC endpoint |

#### Request Telemetry Context

```python
@dataclass
class PipelineStep:
    name: str           # e.g. "embedding", "opa_policy", "vector_search"
    service: str        # e.g. "mcp-server", "reranker"
    duration_ms: float
    status: str         # "ok", "error", "skipped"
    metadata: dict      # step-specific attributes (cache_hit, results_count, etc.)

@dataclass
class RequestTelemetry:
    trace_id: str
    total_ms: float
    steps: list[PipelineStep]

    def to_dict(self) -> dict: ...

# Context variable for async-safe per-request accumulation
_current_telemetry: contextvars.ContextVar[RequestTelemetry | None]

@contextmanager
def request_telemetry(trace_id: str) -> RequestTelemetry:
    """Create and manage per-request telemetry context."""

@contextmanager
def trace_operation(tracer: Tracer, name: str, service: str, **attributes) -> Span:
    """Create OTel span and record step in RequestTelemetry."""
```

`trace_operation` does two things simultaneously:
1. Creates an OTel span with structured attributes (sent to Tempo)
2. Records a `PipelineStep` in the current `RequestTelemetry` (returned in JSON response)

This avoids duplicate timing code.

#### Metrics Aggregator

```python
class MetricsAggregator:
    """Reads from in-process Prometheus registry, returns structured JSON."""

    def __init__(self, service_name: str): ...
    def snapshot(self) -> dict: ...
```

Reads counters, histograms, and gauges from `prometheus_client.REGISTRY`. Returns a service-specific dict with fixed fields (not generic metric names). Percentiles are approximated from histogram buckets using linear interpolation.

### 2. Per-Request Telemetry

Every MCP tool response and proxy chat response includes an optional `_telemetry` field:

```json
{
  "results": ["..."],
  "_telemetry": {
    "trace_id": "abc123def456...",
    "total_ms": 342,
    "steps": [
      {"name": "embedding", "service": "mcp-server", "ms": 45, "status": "ok", "cache_hit": true},
      {"name": "opa_policy", "service": "mcp-server", "ms": 12, "status": "ok", "cache_hit": false, "result": "allow"},
      {"name": "vector_search", "service": "mcp-server", "ms": 120, "status": "ok", "collection": "pb_general", "results": 50},
      {"name": "reranking", "service": "reranker", "ms": 80, "status": "ok", "input": 50, "output": 5},
      {"name": "summarization", "service": "mcp-server", "ms": 85, "status": "ok", "policy": "requested"}
    ]
  }
}
```

Controlled via `TELEMETRY_IN_RESPONSE` (default: `true`). Set to `false` in production to remove telemetry from responses.

#### MCP-Server Pipeline Steps

| Step | Location in code | Span attributes |
|------|-----------------|-----------------|
| `embedding` | `embed_text()` call | `model`, `cache_hit`, `text_length` |
| `opa_policy` | `check_opa_policy()` / `filter_by_policy()` | `role`, `classification`, `result`, `cache_hit` |
| `vector_search` | Qdrant `search()` | `collection`, `top_k`, `results_count` |
| `reranking` | Reranker HTTP call | `input_count`, `output_count`, `fallback` |
| `summarization` | LLM completion call | `model`, `policy` (requested/enforced/denied) |
| `pii_scan` | Ingestion `/scan` call | `entities_found`, `action` |
| `vault_lookup` | Vault HMAC + PG query | `fields_redacted` |
| `graph_query` | AGE Cypher execution | `query_type`, `results_count` |

#### Proxy Pipeline Steps

| Step | Span attributes |
|------|-----------------|
| `auth` | `agent_id`, `agent_role` |
| `pii_pseudonymize` | `entity_types`, `entity_count` |
| `llm_call` | `model`, `provider`, `tokens_in`, `tokens_out` |
| `tool_dispatch` | `tool_name`, `target_server` |
| `agent_loop` | `iterations`, `max_reached` |

The proxy merges its own steps with `_telemetry` from MCP tool responses, producing a combined timeline for the full request path.

#### Reranker Pipeline Steps

| Step | Span attributes |
|------|-----------------|
| `rerank` | `model`, `input_count`, `output_count`, `latency_ms` |

Existing `latency_ms` in rerank response remains. OTel span added for trace propagation.

#### Ingestion Pipeline Steps

| Step | Span attributes |
|------|-----------------|
| `pii_scan` | `entities_found`, `entity_types`, `action` |
| `chunking` | `chunks_count`, `avg_chunk_size` |
| `embedding_batch` | `batch_size`, `cache_hits`, `model` |
| `qdrant_insert` | `collection`, `points_count` |
| `vault_store` | `mappings_count` |
| `layer_generation` | `layer` (L0/L1/L2), `model` |

### 3. JSON Metrics Endpoints

Each service exposes `GET /metrics/json` — structured JSON with fixed fields. No PromQL required.

#### MCP-Server

```json
{
  "service": "mcp-server",
  "uptime_seconds": 3600,
  "requests": {
    "total": 1542,
    "by_tool": {"search_knowledge": 890, "get_code_context": 312},
    "by_status": {"ok": 1500, "error": 42},
    "rate_limited": 5
  },
  "latency": {
    "search_knowledge": {"p50_ms": 280, "p95_ms": 520, "p99_ms": 1200},
    "get_code_context": {"p50_ms": 210, "p95_ms": 400, "p99_ms": 800}
  },
  "policy": {
    "decisions_total": {"allow": 12400, "deny": 180},
    "cache_hit_ratio": 0.72
  },
  "search": {
    "results_avg": {"pb_general": 8.3, "pb_code": 5.1, "pb_rules": 2.0}
  },
  "reranker": {
    "fallbacks_total": 3
  },
  "embedding_cache": {
    "hit_ratio": 0.45,
    "size": 1200,
    "max_size": 2048
  },
  "feedback": {
    "avg_rating_24h": 3.8
  }
}
```

#### Proxy

```json
{
  "service": "pb-proxy",
  "uptime_seconds": 3600,
  "requests": {
    "total": 420,
    "by_model": {"claude-opus": 200, "gpt-4o": 150},
    "by_status": {"ok": 400, "denied": 10, "error": 8, "timeout": 2}
  },
  "latency": {
    "by_model": {"claude-opus": {"p50_ms": 2100, "p95_ms": 4500}}
  },
  "agent_loop": {
    "iterations_avg": 2.3,
    "max_iterations_reached": 5
  },
  "tool_calls": {
    "total": 980,
    "by_tool": {"pb__search_knowledge": 450, "pb__get_code_context": 200}
  },
  "pii": {
    "entities_pseudonymized": {"PERSON": 120, "EMAIL": 45, "PHONE": 12},
    "scan_failures": {"closed": 0, "open": 1}
  }
}
```

#### Reranker

```json
{
  "service": "reranker",
  "uptime_seconds": 3600,
  "requests": {"total": 890, "ok": 885, "error": 5},
  "latency": {"p50_ms": 65, "p95_ms": 120, "p99_ms": 250},
  "batch_size": {"avg": 18.5, "p95": 42},
  "model_load_seconds": 4.2
}
```

#### Ingestion

```json
{
  "service": "ingestion",
  "uptime_seconds": 3600,
  "requests": {"total": 50, "ok": 48, "error": 2},
  "chunks": {"total": 1200, "avg_per_request": 24},
  "pii": {"scans_total": 50, "entities_found": {"PERSON": 30, "EMAIL": 15}},
  "embedding": {"batch_total": 200, "cache_hit_ratio": 0.38}
}
```

### 4. OTel Trace Propagation

#### Trace Flow

```
Client (Demo-UI / Agent)
    │ POST /v1/chat/completions (no traceparent)
    ▼
pb-proxy ──── creates Root Span: "chat_completion"
    │ traceparent: 00-<trace_id>-<span_id>-01
    ▼
pb-mcp-server ──── Child Span: "search_knowledge"
    │ traceparent propagated
    ├──► Qdrant ──── Span around client call (Qdrant has no native OTel)
    ├──► OPA ──── Span around HTTP call (OPA has no native OTel)
    └──► Reranker ──── Child Span: "rerank"
              │
              ▼
           Tempo (all spans collected via OTLP gRPC)
```

#### W3C Trace Context

All inter-service HTTP calls propagate the `traceparent` header automatically via `opentelemetry-instrumentation-httpx`. Services that don't support OTel natively (Qdrant, OPA) get client-side spans wrapping the HTTP call.

#### Auto-Instrumentation vs. Manual Spans

| Type | Scope | Purpose |
|------|-------|---------|
| Auto: `opentelemetry-instrumentation-fastapi` | Server spans | Every incoming HTTP request gets a span automatically |
| Auto: `opentelemetry-instrumentation-httpx` | Client spans | Outgoing HTTP calls propagate `traceparent` automatically |
| Manual: `trace_operation()` | Pipeline logic | Fine-grained steps (embedding, policy check, vector search, etc.) |

Manual spans are nested inside auto-instrumented server spans. The combination gives both high-level request traces and detailed pipeline breakdowns.

### 5. Ingestion Metrics (Gap Fill)

The ingestion service currently exposes zero Prometheus metrics despite being configured as a scrape target in `prometheus.yml`. New metrics:

| Metric | Type | Labels |
|--------|------|--------|
| `pb_ingestion_requests_total` | Counter | `endpoint`, `status` |
| `pb_ingestion_duration_seconds` | Histogram | `endpoint` |
| `pb_ingestion_chunks_total` | Counter | `collection` |
| `pb_ingestion_pii_entities_total` | Counter | `entity_type`, `action` |
| `pb_ingestion_embedding_batch_size` | Histogram | — |

Exposed via `prometheus_client` ASGI app at `/metrics` (same pattern as reranker).

### 6. Dependencies

Added to all service `requirements.txt` files:

```
opentelemetry-api>=1.20
opentelemetry-sdk>=1.20
opentelemetry-exporter-otlp-proto-grpc>=1.20
opentelemetry-instrumentation-fastapi>=0.41b0
opentelemetry-instrumentation-httpx>=0.41b0
```

### 7. Configuration Summary

| Env var | Default | Service | Description |
|---------|---------|---------|-------------|
| `OTEL_ENABLED` | `true` | all | Enable/disable OTel tracing |
| `OTLP_ENDPOINT` | `http://tempo:4317` | all | OTLP gRPC endpoint |
| `TELEMETRY_IN_RESPONSE` | `true` | mcp-server, proxy | Include `_telemetry` in JSON responses |

### 8. Demo-UI Consumption

The powerbrain-demo backend benefits directly:

| Demo feature | Data source | Before | After |
|-------------|-------------|--------|-------|
| Pipeline View | `_telemetry` in chat response | Not possible (no per-request data) | Direct JSON, no parsing needed |
| Request correlation | `trace_id` in `_telemetry` | Not possible (no correlation ID) | Automatic via OTel |
| Dashboard metrics | `GET /metrics/json` per service | PromQL queries against Prometheus | Simple HTTP GET, fixed JSON schema |
| Live event stream | Docker logs + `trace_id` | Temporal correlation only | `trace_id` in structured logs |

The demo-backend needs only `httpx` calls to `/metrics/json` endpoints — no Prometheus dependency, no PromQL knowledge.

## Non-Goals

- Replacing Prometheus (stays as collector/alerting)
- Custom Grafana dashboard work (existing dashboards remain, no new ones)
- Distributed tracing UI in the demo (traces go to Tempo for ops use; demo uses `_telemetry` JSON)
- Token counting or cost tracking (separate feature)
