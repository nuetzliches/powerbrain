# LiteLLM Router Integration

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire up `litellm.Router` so the existing `litellm_config.yaml` is actually loaded, enabling model aliases, fallbacks, and load balancing.

**Architecture:** `proxy.py` loads the YAML config at startup and creates a `litellm.Router` instance (or falls back to direct `litellm.acompletion` if config is empty). `AgentLoop` receives a callable `acompletion` function instead of importing `litellm` directly, making it testable and router-agnostic.

**Tech Stack:** Python 3.12, LiteLLM (Router), PyYAML, pytest

---

### Task 1: Add PyYAML dependency

**Files:**
- Modify: `pb-proxy/requirements.txt`

**Step 1: Add pyyaml to requirements**

Add `pyyaml>=6.0` after the `pydantic` line in `pb-proxy/requirements.txt`:

```
fastapi>=0.115
uvicorn[standard]>=0.34
litellm>=1.60
mcp>=1.0
httpx>=0.27
pydantic>=2.0
pyyaml>=6.0
prometheus-client>=0.21
tenacity>=9.0
```

**Step 2: Commit**

```bash
git add pb-proxy/requirements.txt
git commit -m "chore(proxy): add pyyaml dependency for LiteLLM config loading"
```

---

### Task 2: Refactor AgentLoop to accept a completion callable

The goal is to decouple `AgentLoop` from the global `litellm` module. Instead of
`import litellm` + `litellm.acompletion()`, the loop receives an `acompletion`
callable. This lets `proxy.py` pass either `router.acompletion` or
`litellm.acompletion` depending on config.

**Files:**
- Modify: `pb-proxy/agent_loop.py`
- Modify: `pb-proxy/tests/test_agent_loop.py`

**Step 1: Update the tests to use a callable instead of patching litellm**

Replace the contents of `pb-proxy/tests/test_agent_loop.py` with:

```python
"""Tests for the agent loop (tool-call execution cycle)."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_loop import AgentLoop, AgentLoopResult


@pytest.fixture
def mock_tool_injector():
    injector = MagicMock()
    injector.tool_names = {"search_knowledge", "check_policy"}
    injector.call_tool = AsyncMock(return_value='{"results": []}')
    return injector


def _make_response(tool_calls=None, finish_reason="stop"):
    """Helper to create a mock LLM response."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.tool_calls = tool_calls
    resp.choices[0].message.role = "assistant"
    resp.choices[0].message.content = None if tool_calls else "Hello!"
    resp.choices[0].finish_reason = finish_reason
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    return resp


def _make_tool_call(call_id, name, arguments='{}'):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


@pytest.mark.asyncio
async def test_no_tool_calls_returns_immediately(mock_tool_injector):
    """When LLM responds without tool calls, return immediately."""
    mock_acompletion = AsyncMock(return_value=_make_response())

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Hello"}],
        tools=[],
    )

    assert result.iterations == 1
    assert result.tool_calls_executed == 0
    mock_acompletion.assert_called_once()


@pytest.mark.asyncio
async def test_tool_call_is_executed(mock_tool_injector):
    """When LLM responds with a Powerbrain tool call, execute it."""
    tc = _make_tool_call("call_123", "search_knowledge", '{"query": "test"}')
    mock_acompletion = AsyncMock(side_effect=[
        _make_response(tool_calls=[tc], finish_reason="tool_calls"),
        _make_response(),
    ])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Search for test"}],
        tools=[],
    )

    assert result.iterations == 2
    assert result.tool_calls_executed == 1
    mock_tool_injector.call_tool.assert_called_once_with(
        "search_knowledge", {"query": "test"}
    )


@pytest.mark.asyncio
async def test_max_iterations_stops_loop(mock_tool_injector):
    """Loop stops after max_iterations and returns last response."""
    tc = _make_tool_call("call_loop", "search_knowledge", '{"query": "loop"}')
    response = _make_response(tool_calls=[tc], finish_reason="tool_calls")
    mock_acompletion = AsyncMock(return_value=response)

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=3)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Loop forever"}],
        tools=[],
    )

    assert result.iterations == 3
    assert result.max_iterations_reached is True


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(mock_tool_injector):
    """Unknown tool calls get error results fed back to LLM."""
    tc = _make_tool_call("call_unknown", "unknown_tool")
    mock_acompletion = AsyncMock(side_effect=[
        _make_response(tool_calls=[tc], finish_reason="tool_calls"),
        _make_response(),
    ])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Use unknown"}],
        tools=[],
    )

    mock_tool_injector.call_tool.assert_not_called()
    assert result.iterations == 2


@pytest.mark.asyncio
async def test_tool_call_timeout_feeds_error_to_llm(mock_tool_injector):
    """Tool timeout returns error JSON to LLM, loop continues."""
    mock_tool_injector.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())

    tc = _make_tool_call("call_timeout", "search_knowledge", '{"query": "slow"}')
    mock_acompletion = AsyncMock(side_effect=[
        _make_response(tool_calls=[tc], finish_reason="tool_calls"),
        _make_response(),
    ])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Slow query"}],
        tools=[],
    )

    assert result.iterations == 2
    assert result.tool_calls_executed == 0


@pytest.mark.asyncio
async def test_tool_call_exception_feeds_error_to_llm(mock_tool_injector):
    """Tool exception returns error JSON to LLM, loop continues."""
    mock_tool_injector.call_tool = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )

    tc = _make_tool_call("call_error", "search_knowledge", '{"query": "broken"}')
    mock_acompletion = AsyncMock(side_effect=[
        _make_response(tool_calls=[tc], finish_reason="tool_calls"),
        _make_response(),
    ])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Broken query"}],
        tools=[],
    )

    assert result.iterations == 2
    assert result.tool_calls_executed == 0
```

**Step 2: Run tests to verify they fail**

Run: `cd pb-proxy && python -m pytest tests/test_agent_loop.py -v`
Expected: FAIL — `AgentLoop.__init__()` does not accept `acompletion` parameter

**Step 3: Update AgentLoop implementation**

Replace the contents of `pb-proxy/agent_loop.py` with:

```python
"""
Agent loop: executes tool calls from LLM responses against
the Powerbrain MCP server and re-submits results until
the LLM produces a final response.
"""

import json
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Any

from tool_injection import ToolInjector
import config

log = logging.getLogger("pb-proxy.loop")

# Type alias for the completion callable (litellm.acompletion or router.acompletion)
ACompletion = Callable[..., Any]


@dataclass
class AgentLoopResult:
    """Result of an agent loop execution."""
    response: object                        # Final LiteLLM response
    iterations: int = 0                     # Number of LLM calls made
    tool_calls_executed: int = 0            # Total tool calls executed
    max_iterations_reached: bool = False    # True if loop was cut short
    tools_used: list[str] = field(default_factory=list)


class AgentLoop:
    """Executes the tool-call loop between LLM and MCP server."""

    def __init__(
        self,
        tool_injector: ToolInjector,
        acompletion: ACompletion,
        max_iterations: int = 10,
        tool_call_timeout: int | None = None,
    ) -> None:
        self._injector = tool_injector
        self._acompletion = acompletion
        self._max_iterations = max_iterations
        self._tool_call_timeout = tool_call_timeout or config.TOOL_CALL_TIMEOUT

    async def run(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        **litellm_kwargs,
    ) -> AgentLoopResult:
        """Run the agent loop until final response or max iterations."""
        result = AgentLoopResult(response=None)
        working_messages = list(messages)

        for iteration in range(1, self._max_iterations + 1):
            result.iterations = iteration

            # Call LLM via injected completion callable
            response = await self._acompletion(
                model=model,
                messages=working_messages,
                tools=tools if tools else None,
                **litellm_kwargs,
            )

            choice = response.choices[0]

            # No tool calls → final response
            if not choice.message.tool_calls:
                result.response = response
                return result

            # Append assistant message with tool calls
            assistant_msg = {
                "role": "assistant",
                "content": choice.message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in choice.message.tool_calls
                ],
            }
            working_messages.append(assistant_msg)

            # Execute each tool call
            for tc in choice.message.tool_calls:
                tool_name = tc.function.name
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON in tool arguments for %s: %s",
                                tool_name, tc.function.arguments)
                    arguments = {}

                if tool_name in self._injector.tool_names:
                    # Execute against MCP server
                    try:
                        tool_result = await asyncio.wait_for(
                            self._injector.call_tool(tool_name, arguments),
                            timeout=self._tool_call_timeout,
                        )
                        result.tool_calls_executed += 1
                        result.tools_used.append(tool_name)
                    except asyncio.TimeoutError:
                        tool_result = json.dumps({
                            "error": f"Tool '{tool_name}' timed out after "
                                     f"{self._tool_call_timeout}s"
                        })
                        log.warning("Tool call timed out: %s", tool_name)
                    except Exception as e:
                        tool_result = json.dumps({
                            "error": f"Tool '{tool_name}' failed: {str(e)}"
                        })
                        log.error("Tool call failed: %s — %s", tool_name, e)
                else:
                    # Unknown tool
                    tool_result = json.dumps({
                        "error": f"Unknown tool '{tool_name}'. "
                                 f"Available tools: {sorted(self._injector.tool_names)}"
                    })
                    log.warning("Unknown tool requested: %s", tool_name)

                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

        # Max iterations reached
        result.max_iterations_reached = True
        result.response = response
        log.warning("Agent loop reached max iterations (%d)", self._max_iterations)
        return result
```

**Step 4: Run tests to verify they pass**

Run: `cd pb-proxy && python -m pytest tests/test_agent_loop.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add pb-proxy/agent_loop.py pb-proxy/tests/test_agent_loop.py
git commit -m "refactor(proxy): decouple AgentLoop from litellm module

AgentLoop now accepts an acompletion callable instead of importing
litellm directly. This enables Router injection from proxy.py."
```

---

### Task 3: Load litellm_config.yaml and create Router in proxy.py

**Files:**
- Modify: `pb-proxy/proxy.py`
- Modify: `pb-proxy/tests/test_proxy.py`

**Step 1: Update proxy tests to mock the router**

In `pb-proxy/tests/test_proxy.py`, update the `mock_deps` fixture to also
patch the new `llm_acompletion` global:

Replace the fixture:

```python
@pytest.fixture
def mock_deps():
    """Mock all external dependencies."""
    with patch("proxy.tool_injector") as mock_injector, \
         patch("proxy.check_opa_policy") as mock_opa, \
         patch("proxy.AgentLoop") as mock_loop_cls, \
         patch("proxy.llm_acompletion") as mock_acompletion:

        mock_injector.merge_tools = MagicMock(return_value=[
            {"type": "function", "function": {"name": "search_knowledge"}},
        ])
        mock_injector.tool_names = {"search_knowledge"}
        mock_opa.return_value = {"provider_allowed": True, "max_iterations": 10}

        mock_result = MagicMock()
        mock_result.response = MagicMock()
        mock_result.response.model_dump.return_value = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"content": "Hello!"}}],
        }
        mock_result.iterations = 1
        mock_result.tool_calls_executed = 0
        mock_result.max_iterations_reached = False
        mock_result.tools_used = []

        mock_loop = AsyncMock()
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_loop_cls.return_value = mock_loop

        yield {
            "injector": mock_injector,
            "opa": mock_opa,
            "loop_cls": mock_loop_cls,
            "loop": mock_loop,
            "result": mock_result,
            "acompletion": mock_acompletion,
        }
```

Add a test that verifies the router callable is passed to AgentLoop:

```python
def test_agent_loop_receives_acompletion(client, mock_deps):
    """AgentLoop is created with the llm_acompletion callable."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 200
    # Verify AgentLoop was constructed with the acompletion callable
    mock_deps["loop_cls"].assert_called_once()
    call_kwargs = mock_deps["loop_cls"].call_args
    assert call_kwargs.kwargs.get("acompletion") is mock_deps["acompletion"]
```

**Step 2: Run tests to verify they fail**

Run: `cd pb-proxy && python -m pytest tests/test_proxy.py -v`
Expected: FAIL — `proxy` module has no `llm_acompletion` attribute

**Step 3: Update proxy.py to load Router from YAML**

In `pb-proxy/proxy.py`, make these changes:

a) Add `yaml` import at the top (after `import time`):
```python
import yaml
```

b) Replace the globals section with:
```python
# ── Globals ──────────────────────────────────────────────────

tool_injector = ToolInjector()
http_client: httpx.AsyncClient | None = None
llm_acompletion: Any = None  # Set in lifespan: router.acompletion or litellm.acompletion
```

c) Add a `_load_llm_router` function before the lifespan:
```python
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
```

d) Update the lifespan to call `_load_llm_router`:

Add `global llm_acompletion` at the start (next to `global http_client`).
After `http_client = ...`, add:
```python
    llm_acompletion = _load_llm_router()
```

e) Update the `chat_completions` endpoint — change the AgentLoop construction from:
```python
    loop = AgentLoop(tool_injector, max_iterations=max_iterations)
```
to:
```python
    loop = AgentLoop(tool_injector, acompletion=llm_acompletion, max_iterations=max_iterations)
```

**Step 4: Run tests to verify they pass**

Run: `cd pb-proxy && python -m pytest tests/test_proxy.py tests/test_agent_loop.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/tests/test_proxy.py
git commit -m "feat(proxy): load LiteLLM Router from litellm_config.yaml

The proxy now loads model_list from the YAML config and creates a
litellm.Router for model aliases, fallbacks, and load balancing.
Falls back to direct litellm.acompletion if config is empty."
```

---

### Task 4: Update litellm_config.yaml with a working example

**Files:**
- Modify: `pb-proxy/litellm_config.yaml`

**Step 1: Add a commented local-Ollama example as active default**

Replace `pb-proxy/litellm_config.yaml`:

```yaml
# LiteLLM Model Configuration for Powerbrain AI Provider Proxy
# Docs: https://docs.litellm.ai/docs/proxy/configs
#
# Each entry maps a model_name (used in API requests) to a
# provider-specific model identifier + credentials.
#
# The proxy loads this file at startup via LITELLM_CONFIG env var.
# If model_list is empty, the proxy falls back to direct LiteLLM
# routing (model name = provider/model, e.g. "openai/gpt-4o").

model_list:
  # ── Local Ollama (works out of the box with Docker Compose) ──
  # - model_name: "llama"
  #   litellm_params:
  #     model: "ollama/llama3.2"
  #     api_base: "http://ollama:11434"

  # ── OpenAI ─────────────────────────────────────────────────
  # - model_name: "gpt-4o"
  #   litellm_params:
  #     model: "openai/gpt-4o"
  #     api_key: "os.environ/OPENAI_API_KEY"

  # ── Anthropic ──────────────────────────────────────────────
  # - model_name: "claude-sonnet"
  #   litellm_params:
  #     model: "anthropic/claude-sonnet-4-20250514"
  #     api_key: "os.environ/ANTHROPIC_API_KEY"
```

**Step 2: Commit**

```bash
git add pb-proxy/litellm_config.yaml
git commit -m "docs(proxy): improve litellm_config.yaml with usage comments"
```

---

### Task 5: Update README proxy section

**Files:**
- Modify: `README.md`

**Step 1: Fix the proxy Quick Start section**

Find the proxy section in README.md (around line 87-97) and update the
comment to be accurate:

Change:
```bash
# Configure your LLM provider(s) in pb-proxy/litellm_config.yaml
docker compose --profile proxy up -d

# Now use OpenAI-compatible API with automatic Powerbrain tools:
```

To:
```bash
# 1. Uncomment/add your LLM provider in pb-proxy/litellm_config.yaml
# 2. Set API keys in .env (e.g. OPENAI_API_KEY=sk-...)
docker compose --profile proxy up -d

# Use the proxy — Powerbrain tools are injected automatically:
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: clarify proxy setup steps in README"
```
