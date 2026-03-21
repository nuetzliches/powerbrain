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
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from prometheus_client import (
    Counter, Histogram, Gauge,
    start_http_server as prom_start_http_server,
)

import config
from tool_injection import ToolInjector
from agent_loop import AgentLoop, AgentLoopResult

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

# ── Globals ──────────────────────────────────────────────────

tool_injector = ToolInjector()
http_client: httpx.AsyncClient | None = None


# ── OPA Helper ───────────────────────────────────────────────

async def check_opa_policy(agent_role: str, provider: str) -> dict:
    """Check proxy policies via OPA."""
    assert http_client is not None
    opa_input = {
        "input": {
            "agent_role": agent_role,
            "provider": provider,
        }
    }
    try:
        resp = await http_client.post(
            f"{config.OPA_URL}/v1/data/kb/proxy",
            json=opa_input,
        )
        resp.raise_for_status()
        return resp.json().get("result", {})
    except Exception as e:
        log.error("OPA policy check failed: %s", e)
        # Fail closed: deny if OPA is unreachable
        return {"provider_allowed": False, "max_iterations": 0}


# ── Request/Response Models ──────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


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


# ── Application ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    prom_start_http_server(config.METRICS_PORT)
    log.info("Prometheus metrics on port %d", config.METRICS_PORT)

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
    await http_client.aclose()
    log.info("pb-proxy shut down")


app = FastAPI(
    title="Powerbrain AI Provider Proxy",
    description="Transparent tool injection proxy for LLM providers",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "tools_loaded": len(tool_injector.tool_names),
        "fail_mode": config.FAIL_MODE,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    start_time = time.monotonic()

    # Streaming not yet supported
    if request.stream:
        raise HTTPException(
            status_code=501,
            detail="Streaming not yet supported. Set stream=false.",
        )

    # TODO: Extract agent_role from auth (for now default to "developer")
    agent_role = "developer"

    # OPA policy check
    policy = await check_opa_policy(agent_role, request.model)
    if not policy.get("provider_allowed", False):
        PROXY_REQUESTS.labels(model=request.model, status="denied").inc()
        raise HTTPException(
            status_code=403,
            detail=f"Provider '{request.model}' not allowed for role '{agent_role}'",
        )

    max_iterations = policy.get("max_iterations", config.MAX_ITERATIONS)

    # Merge Powerbrain tools into request
    merged_tools = tool_injector.merge_tools(request.tools)

    # Build LiteLLM kwargs from extra fields
    litellm_kwargs: dict[str, Any] = {}
    if request.temperature is not None:
        litellm_kwargs["temperature"] = request.temperature
    if request.max_tokens is not None:
        litellm_kwargs["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        litellm_kwargs["top_p"] = request.top_p

    # Run agent loop
    loop = AgentLoop(tool_injector, max_iterations=max_iterations)
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
        raise HTTPException(status_code=502, detail=f"LLM request failed: {str(e)}")

    # Metrics
    latency = time.monotonic() - start_time
    PROXY_REQUESTS.labels(model=request.model, status="ok").inc()
    PROXY_LATENCY.labels(model=request.model).observe(latency)
    PROXY_ITERATIONS.observe(result.iterations)
    for tool_name in result.tools_used:
        PROXY_TOOL_CALLS.labels(tool_name=tool_name).inc()

    # Build response
    response_data = result.response.model_dump()

    # Add proxy metadata headers
    headers = {
        "X-Proxy-Iterations": str(result.iterations),
        "X-Proxy-Tool-Calls": str(result.tool_calls_executed),
    }
    if result.max_iterations_reached:
        headers["X-Proxy-Max-Iterations-Reached"] = "true"

    from fastapi.responses import JSONResponse
    return JSONResponse(content=response_data, headers=headers)


# ── Entrypoint ───────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host=config.PROXY_HOST, port=config.PROXY_PORT)
