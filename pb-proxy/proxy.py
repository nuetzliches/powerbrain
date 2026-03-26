"""
Powerbrain AI Provider Proxy
==============================
Optional gateway that sits between AI consumers and LLM providers.
Transparently injects Powerbrain MCP tools into every LLM request
and executes tool calls via the MCP server.

Activation: docker compose --profile proxy up -d
Endpoint: POST /v1/chat/completions (OpenAI-compatible)
"""

import asyncio
import json
import logging
import re
import sys
import time
import uuid as _uuid
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from prometheus_client import (
    Counter, Histogram,
    start_http_server as prom_start_http_server,
)

import config
from middleware import ProxyAuthMiddleware

# Try to import telemetry - fallback if not available (for tests)
try:
    shared_path = os.path.join(os.path.dirname(__file__), "..", "shared")
    if shared_path not in sys.path:
        sys.path.insert(0, shared_path)
    from shared.telemetry import (
        init_telemetry, setup_auto_instrumentation, trace_operation,
        request_telemetry_context, get_current_telemetry,
        MetricsAggregator, TELEMETRY_IN_RESPONSE,
    )
except ImportError:
    # Mock for tests
    from contextlib import nullcontext
    def init_telemetry(service_name):
        return None
    def setup_auto_instrumentation(app):
        pass
    def trace_operation(*args, **kwargs):
        return nullcontext()
    def request_telemetry_context(trace_id):
        return nullcontext()
    def get_current_telemetry():
        return None
    class MetricsAggregator:
        def __init__(self, service_name): 
            self.service_name = service_name
        def snapshot(self): 
            return {"service": self.service_name, "uptime_seconds": 0, "raw_metrics": {}}
        def histogram_percentiles(self, *args, **kwargs): 
            return {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0}
    TELEMETRY_IN_RESPONSE = False
from tool_injection import ToolInjector
from agent_loop import AgentLoop, AgentLoopResult
from pii_middleware import (
    pseudonymize_messages,
    depseudonymize_text,
    filter_non_text_content,
    generate_session_salt,
    build_system_hint,
)
from auth import ProxyKeyVerifier
from middleware import ProxyAuthMiddleware

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("pb-proxy")

# ── Prometheus Metrics ───────────────────────────────────────

PROXY_REQUESTS = Counter(
    "pbproxy_requests_total",
    "Total proxy requests",
    ["model", "status"],
)
PROXY_LATENCY = Histogram(
    "pbproxy_request_latency_seconds",
    "Proxy request latency",
    ["model"],
)
PROXY_TOOL_CALLS = Counter(
    "pbproxy_tool_calls_total",
    "Tool calls executed by proxy",
    ["tool_name"],
)
PROXY_ITERATIONS = Histogram(
    "pbproxy_loop_iterations",
    "Agent loop iterations per request",
)
PII_ENTITIES_PSEUDONYMIZED = Counter(
    "pbproxy_pii_entities_pseudonymized_total",
    "Total PII entities pseudonymized in chat messages",
    ["entity_type"],
)
PII_SCAN_FAILURES = Counter(
    "pbproxy_pii_scan_failures_total",
    "PII scan failures (ingestion service unreachable)",
    ["fail_mode"],
)

# ── Globals ──────────────────────────────────────────────────

tool_injector = ToolInjector()
http_client: httpx.AsyncClient | None = None
pii_http_client: httpx.AsyncClient | None = None
router_acompletion: Any = None      # LiteLLM Router (for aliases)
direct_acompletion: Any = None      # LiteLLM direct (for passthrough)
model_list: list[dict[str, Any]] = []
known_aliases: set[str] = set()     # Model names configured in Router
key_verifier = ProxyKeyVerifier()


# ── OPA Helper ───────────────────────────────────────────────

async def check_opa_policy(
    agent_role: str, provider: str, configured_servers: list[str],
) -> dict:
    """Check proxy policies via OPA."""
    if http_client is None:
        raise RuntimeError("http_client not initialized (lifespan not started)")
    opa_input = {
        "input": {
            "agent_role": agent_role,
            "provider": provider,
            "configured_servers": configured_servers,
        }
    }
    try:
        resp = await http_client.post(
            f"{config.OPA_URL}/v1/data/pb/proxy",
            json=opa_input,
        )
        resp.raise_for_status()
        return resp.json().get("result", {})
    except Exception as e:
        log.error("OPA policy check failed: %s", e)
        # Fail closed: deny if OPA is unreachable
        return {"provider_allowed": False, "max_iterations": 0}


# ── Request/Response Models ──────────────────────────────────

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    tools: list[dict] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stream: bool = False

    # Pass through any additional parameters
    model_config = {"extra": "allow"}


# ── Helper Functions ─────────────────────────────────────────

def _extract_provider(model: str) -> str | None:
    """Extract provider prefix from model string.
    
    'anthropic/claude-opus-4-20250514' → 'anthropic'
    'gpt-4o' (alias) → None
    """
    if "/" in model:
        return model.split("/")[0]
    return None


# ── LLM Router ───────────────────────────────────────────────

def _load_llm_router() -> tuple[Any | None, Any, list[dict[str, Any]], set[str]]:
    """Load LiteLLM Router from YAML config + direct fallback.

    Returns (router_acompletion_or_None, direct_acompletion, model_list, known_aliases).
    """
    import litellm

    config_path = config.LITELLM_CONFIG
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("LiteLLM config not found at %s, using direct completion only", config_path)
        return None, litellm.acompletion, [], set()

    models = cfg.get("model_list", [])
    if not models:
        log.info("LiteLLM config has empty model_list, using direct completion only")
        return None, litellm.acompletion, [], set()

    router = litellm.Router(model_list=models)
    aliases = {m.get("model_name", "") for m in models}
    log.info("LiteLLM Router loaded with %d alias(es): %s", len(aliases), sorted(aliases))
    log.info("Passthrough routing enabled for providers: %s", sorted(config.PROVIDER_KEY_MAP.keys()))
    return router.acompletion, litellm.acompletion, models, aliases


# ── Application ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, pii_http_client, router_acompletion, direct_acompletion, model_list, known_aliases
    http_client = httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    pii_http_client = httpx.AsyncClient(timeout=10)
    router_acompletion, direct_acompletion, model_list, known_aliases = _load_llm_router()
    prom_start_http_server(config.METRICS_PORT)
    log.info("Prometheus metrics on port %d", config.METRICS_PORT)

    if config.AUTH_REQUIRED:
        await key_verifier.start()
        log.info("Proxy authentication enabled (AUTH_REQUIRED=true)")
    else:
        log.warning("Proxy authentication DISABLED (AUTH_REQUIRED=false)")

    try:
        await tool_injector.start()
    except Exception as e:
        if config.FAIL_MODE == "closed":
            log.error("Cannot start: MCP server unreachable and FAIL_MODE=closed: %s", e)
            raise
        log.warning("MCP server unreachable, starting in degraded mode: %s", e)

    log.info("pb-proxy started on %s:%d", config.PROXY_HOST, config.PROXY_PORT)
    yield

    await tool_injector.stop()
    if config.AUTH_REQUIRED:
        await key_verifier.stop()
    await http_client.aclose()
    await pii_http_client.aclose()
    log.info("pb-proxy shut down")


app = FastAPI(
    title="Powerbrain AI Provider Proxy",
    description="Transparent tool injection proxy for LLM providers",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(ProxyAuthMiddleware, key_verifier=key_verifier)

_proxy_tracer = init_telemetry("pb-proxy")
setup_auto_instrumentation(app)
_proxy_metrics = MetricsAggregator("pb-proxy")


@app.get("/health")
async def health():
    tools_loaded = len(tool_injector.tool_names)
    status = "healthy" if tools_loaded > 0 else "degraded"
    return {
        "status": status,
        "tools_loaded": tools_loaded,
        "fail_mode": config.FAIL_MODE,
    }


@app.get("/v1/models")
async def list_models():
    """OpenAI-compatible model listing.

    Returns models configured in litellm_config.yaml so that
    clients (OpenCode, Cursor, etc.) can discover available models.
    """
    data = []
    for entry in model_list:
        model_name = entry.get("model_name", "unknown")
        params = entry.get("litellm_params", {})
        provider_model = params.get("model", "")
        # Extract provider prefix (e.g. "github/gpt-4o" → "github")
        owner = provider_model.split("/")[0] if "/" in provider_model else "powerbrain-proxy"
        data.append({
            "id": model_name,
            "object": "model",
            "created": 0,
            "owned_by": owner,
        })
    return {"object": "list", "data": data}


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


def _resolve_provider_key(model: str) -> tuple[Any, dict[str, Any]]:
    """Determine which acompletion callable + extra kwargs to use.

    For known aliases: use Router.
    For provider/model format: use direct litellm.acompletion with resolved key.
    Returns (acompletion_callable, extra_kwargs).
    Raises HTTPException if model can't be routed.
    """
    extra_kwargs: dict[str, Any] = {}

    if model in known_aliases:
        acompletion = router_acompletion or direct_acompletion
        return acompletion, extra_kwargs

    # Passthrough: model must be "provider/model-name" format
    if "/" not in model:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown model '{model}'. "
                f"Use 'provider/model-name' format (e.g. 'anthropic/claude-opus-4-20250514') "
                f"or one of the configured aliases: {sorted(known_aliases)}"
            ),
        )

    provider = model.split("/")[0]

    # Resolve API key: provider env var → reject
    if provider in config.PROVIDER_KEY_MAP:
        extra_kwargs["api_key"] = config.PROVIDER_KEY_MAP[provider]
    else:
        raise HTTPException(
            status_code=401,
            detail=f"No API key configured for provider '{provider}'. "
                   f"Configure {provider.upper()}_API_KEY as env var / Docker Secret.",
        )

    return direct_acompletion, extra_kwargs


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    start_time = time.monotonic()
    is_streaming = request.stream
    trace_id = _uuid.uuid4().hex[:16]

    with request_telemetry_context(trace_id) as req_telemetry:
        # ── Authentication (via middleware) ──────────────────────────
        agent_id = getattr(raw_request.state, "agent_id", "anonymous")
        agent_role = getattr(raw_request.state, "agent_role", "developer")
        user_api_key = getattr(raw_request.state, "bearer_token", None)

        # OPA policy check
        policy = await check_opa_policy(
            agent_role, request.model, tool_injector.server_names,
        )
        if not policy.get("provider_allowed", False):
            PROXY_REQUESTS.labels(model=request.model, status="denied").inc()
            raise HTTPException(
                status_code=403,
                detail=f"Provider '{request.model}' not allowed for role '{agent_role}'",
            )

        max_iterations = policy.get("max_iterations", config.MAX_ITERATIONS)

        allowed_servers = policy.get("mcp_servers_allowed", tool_injector.server_names)

        # ── PII Protection ───────────────────────────────────────
        pii_reverse_map: dict[str, str] = {}
        pii_enabled = policy.get("pii_scan_enabled", config.PII_SCAN_ENABLED)
        pii_forced = policy.get("pii_scan_forced", config.PII_SCAN_FORCED)

        if pii_enabled:
            with trace_operation(_proxy_tracer, "pii_pseudonymize", "pb-proxy"):
                # Filter non-text content first (images, files)
                non_text_action = policy.get("non_text_content_action", "placeholder")
                try:
                    filtered_messages, had_non_text = filter_non_text_content(
                        request.messages, action=non_text_action
                    )
                    if had_non_text:
                        request.messages = filtered_messages
                        log.info("Non-text content filtered (action=%s)", non_text_action)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))

                # Pseudonymize PII in messages
                session_salt = generate_session_salt()
                try:
                    pseudonymized_messages, pii_reverse_map = await pseudonymize_messages(
                        request.messages, session_salt, pii_http_client
                    )
                    # Inject system hint if PII was found
                    if pii_reverse_map:
                        entity_types = list({
                            m.group(1) for m in re.finditer(
                                r"\[([A-Z_]+):[a-f0-9]{8}\]",
                                " ".join(
                                    m.get("content", "") for m in pseudonymized_messages
                                    if isinstance(m.get("content"), str)
                                ),
                            )
                        })
                        for et in entity_types:
                            PII_ENTITIES_PSEUDONYMIZED.labels(entity_type=et).inc()
                        hint = build_system_hint(entity_types)
                        if hint:
                            pseudonymized_messages.insert(0, {
                                "role": "system",
                                "content": hint,
                            })
                    request.messages = pseudonymized_messages
                except Exception as e:
                    if pii_forced:
                        log.error("PII scan forced but failed: %s", e)
                        PII_SCAN_FAILURES.labels(fail_mode="closed").inc()
                        raise HTTPException(
                            status_code=503,
                            detail="PII protection required but scanner unavailable",
                        )
                    PII_SCAN_FAILURES.labels(fail_mode="open").inc()
                    log.warning("PII scan failed (non-forced, continuing): %s", e)

        # Merge Powerbrain tools into request (skip if disabled or client sends own tools)
        if config.TOOL_INJECTION_ENABLED:
            merged_tools = tool_injector.merge_tools(
                request.tools, allowed_servers=allowed_servers,
            )
        else:
            merged_tools = request.tools or []

        # Build LiteLLM kwargs from extra fields
        litellm_kwargs: dict[str, Any] = {}
        if request.temperature is not None:
            litellm_kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            # Cap max_tokens to avoid provider rejections (GitHub Models: 16384)
            litellm_kwargs["max_tokens"] = min(request.max_tokens, 16384)
        if request.top_p is not None:
            litellm_kwargs["top_p"] = request.top_p

        # Resolve routing: alias → Router, provider/model → direct
        acompletion, routing_kwargs = _resolve_provider_key(model=request.model)
        litellm_kwargs.update(routing_kwargs)

        # Run agent loop
        with trace_operation(_proxy_tracer, "agent_loop", "pb-proxy", model=request.model):
            loop = AgentLoop(
                tool_injector,
                acompletion=acompletion,
                max_iterations=max_iterations,
                pii_reverse_map=pii_reverse_map,
                user_token=user_api_key,
            )
            try:
                result: AgentLoopResult = await asyncio.wait_for(
                    loop.run(
                        model=request.model,
                        messages=request.messages,
                        tools=merged_tools,
                        **litellm_kwargs,
                    ),
                    timeout=config.REQUEST_TIMEOUT,
                )
            except asyncio.TimeoutError:
                PROXY_REQUESTS.labels(model=request.model, status="timeout").inc()
                raise HTTPException(status_code=504, detail="Request timed out")
            except Exception as e:
                PROXY_REQUESTS.labels(model=request.model, status="error").inc()
                log.error("Agent loop failed: %s", e)
                raise HTTPException(status_code=502, detail="LLM request failed")

        # Metrics
        latency = time.monotonic() - start_time
        PROXY_REQUESTS.labels(model=request.model, status="ok").inc()
        PROXY_LATENCY.labels(model=request.model).observe(latency)
        PROXY_ITERATIONS.observe(result.iterations)
        for tool_name in result.tools_used:
            PROXY_TOOL_CALLS.labels(tool_name=tool_name).inc()

        # Build response
        response_data = result.response.model_dump()

        # ── De-pseudonymize response ─────────────────────────────
        if pii_reverse_map:
            for choice in response_data.get("choices", []):
                msg = choice.get("message", {})
                if isinstance(msg.get("content"), str):
                    msg["content"] = depseudonymize_text(msg["content"], pii_reverse_map)

        # Add telemetry to response if enabled
        if TELEMETRY_IN_RESPONSE:
            rt = get_current_telemetry()
            if rt is not None:
                rt.finish()
                response_data["_telemetry"] = rt.to_dict()

        # Add proxy metadata headers
        headers = {
            "X-Proxy-Iterations": str(result.iterations),
            "X-Proxy-Tool-Calls": str(result.tool_calls_executed),
        }
        if result.max_iterations_reached:
            headers["X-Proxy-Max-Iterations-Reached"] = "true"

        if is_streaming:
            return StreamingResponse(
                _generate_sse_stream(response_data),
                media_type="text/event-stream",
                headers={
                    **headers,
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return JSONResponse(content=response_data, headers=headers)


# ── SSE Streaming Helpers ────────────────────────────────────

def _sse_chunk(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


async def _generate_sse_stream(
    response_data: dict,
) -> AsyncGenerator[str, None]:
    """Convert a complete chat response to SSE stream chunks.

    Splits the content into small pieces so the client sees
    incremental output, matching OpenAI's streaming format.
    """
    chat_id = response_data.get("id", f"chatcmpl-proxy-{int(time.time())}")
    model = response_data.get("model", "unknown")
    created = response_data.get("created", int(time.time()))

    for choice in response_data.get("choices", []):
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        index = choice.get("index", 0)
        finish_reason = choice.get("finish_reason", "stop")

        # Chunk 1: role
        yield _sse_chunk({
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": index,
                "delta": {"role": msg.get("role", "assistant")},
                "finish_reason": None,
            }],
        })

        # Content chunks: split into ~20-char pieces
        if content:
            chunk_size = 20
            for i in range(0, len(content), chunk_size):
                piece = content[i : i + chunk_size]
                yield _sse_chunk({
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": index,
                        "delta": {"content": piece},
                        "finish_reason": None,
                    }],
                })

        # Final chunk: finish_reason
        yield _sse_chunk({
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": index,
                "delta": {},
                "finish_reason": finish_reason,
            }],
        })

    # Usage chunk (some clients expect this)
    usage = response_data.get("usage")
    if usage:
        yield _sse_chunk({
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": usage,
        })

    yield "data: [DONE]\n\n"


# ── Entrypoint ───────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host=config.PROXY_HOST, port=config.PROXY_PORT)
