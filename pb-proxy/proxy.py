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
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from prometheus_client import (
    Counter, Histogram,
    start_http_server as prom_start_http_server,
)

import config
from tool_injection import ToolInjector
from agent_loop import AgentLoop, AgentLoopResult
from pii_middleware import (
    pseudonymize_messages,
    depseudonymize_text,
    filter_non_text_content,
    generate_session_salt,
    build_system_hint,
)

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
llm_acompletion: Any = None


# ── OPA Helper ───────────────────────────────────────────────

async def check_opa_policy(agent_role: str, provider: str) -> dict:
    """Check proxy policies via OPA."""
    if http_client is None:
        raise RuntimeError("http_client not initialized (lifespan not started)")
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


# ── LLM Router ───────────────────────────────────────────────

def _load_llm_router() -> Any:
    """Load LiteLLM Router from YAML config. Falls back to litellm.acompletion."""
    import litellm

    config_path = config.LITELLM_CONFIG
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("LiteLLM config not found at %s, using direct completion", config_path)
        return litellm.acompletion

    model_list = cfg.get("model_list", [])
    if not model_list:
        log.info("LiteLLM config has empty model_list, using direct completion")
        return litellm.acompletion

    router = litellm.Router(model_list=model_list)
    log.info("LiteLLM Router loaded with %d model(s): %s",
             len(model_list),
             [m.get("model_name", "?") for m in model_list])
    return router.acompletion


# ── Application ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, pii_http_client, llm_acompletion
    http_client = httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT)
    pii_http_client = httpx.AsyncClient(timeout=10)
    llm_acompletion = _load_llm_router()
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
    await pii_http_client.aclose()
    log.info("pb-proxy shut down")


app = FastAPI(
    title="Powerbrain AI Provider Proxy",
    description="Transparent tool injection proxy for LLM providers",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    tools_loaded = len(tool_injector.tool_names)
    status = "healthy" if tools_loaded > 0 else "degraded"
    return {
        "status": status,
        "tools_loaded": tools_loaded,
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

    # ── PII Protection ───────────────────────────────────────
    pii_reverse_map: dict[str, str] = {}
    pii_enabled = policy.get("pii_scan_enabled", config.PII_SCAN_ENABLED)
    pii_forced = policy.get("pii_scan_forced", config.PII_SCAN_FORCED)

    if pii_enabled:
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
    loop = AgentLoop(
        tool_injector,
        acompletion=llm_acompletion,
        max_iterations=max_iterations,
        pii_reverse_map=pii_reverse_map,
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

    # Add proxy metadata headers
    headers = {
        "X-Proxy-Iterations": str(result.iterations),
        "X-Proxy-Tool-Calls": str(result.tool_calls_executed),
    }
    if result.max_iterations_reached:
        headers["X-Proxy-Max-Iterations-Reached"] = "true"

    return JSONResponse(content=response_data, headers=headers)


# ── Entrypoint ───────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host=config.PROXY_HOST, port=config.PROXY_PORT)
