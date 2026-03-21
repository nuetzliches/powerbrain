# AI Provider Proxy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an optional AI Provider Proxy (`pb-proxy`) that transparently injects Powerbrain MCP tools into LLM requests and executes tool calls, activated via Docker Compose profile.

**Architecture:** FastAPI service using LiteLLM for multi-provider routing and the MCP SDK client to connect to the existing Powerbrain MCP server. Sits behind the `proxy` Docker Compose profile. OPA policies control which tools are injected and which providers are allowed.

**Tech Stack:** Python 3.12, FastAPI, LiteLLM, MCP SDK (client), httpx, Pydantic, OPA (Rego), Docker

---

### Task 1: OPA Proxy Policies

**Files:**
- Create: `opa-policies/kb/proxy.rego`
- Create: `opa-policies/kb/test_proxy.rego`

**Step 1: Write the OPA test file**

Create `opa-policies/kb/test_proxy.rego`:

```rego
package kb.proxy_test

import rego.v1
import data.kb.proxy

# ── provider_allowed ─────────────────────────────────────────

test_provider_allowed_for_analyst if {
    proxy.provider_allowed with input as {
        "agent_role": "analyst",
        "provider": "gpt-4o",
    }
}

test_provider_allowed_for_admin if {
    proxy.provider_allowed with input as {
        "agent_role": "admin",
        "provider": "gpt-4o",
    }
}

test_provider_allowed_for_developer if {
    proxy.provider_allowed with input as {
        "agent_role": "developer",
        "provider": "gpt-4o",
    }
}

test_provider_denied_for_viewer if {
    not proxy.provider_allowed with input as {
        "agent_role": "viewer",
        "provider": "gpt-4o",
    }
}

# ── required_tools ───────────────────────────────────────────

test_required_tools_default if {
    proxy.required_tools == {"search_knowledge", "check_policy"} with input as {
        "agent_role": "analyst",
    }
}

# ── max_iterations ───────────────────────────────────────────

test_max_iterations_analyst if {
    proxy.max_iterations == 5 with input as {
        "agent_role": "analyst",
    }
}

test_max_iterations_developer if {
    proxy.max_iterations == 10 with input as {
        "agent_role": "developer",
    }
}

test_max_iterations_admin if {
    proxy.max_iterations == 10 with input as {
        "agent_role": "admin",
    }
}
```

**Step 2: Run tests to verify they fail**

Run: `docker exec kb-opa /opa test /policies/kb/ -v --run 'proxy'`
Expected: FAIL (proxy package not found)

**Step 3: Write the policy**

Create `opa-policies/kb/proxy.rego`:

```rego
# ============================================================
#  Powerbrain – AI Provider Proxy Policies
#  Package: kb.proxy
#
#  Controls the AI Provider Proxy behavior:
#  - Which agent roles may use the proxy
#  - Which MCP tools are mandatory (injected into every request)
#  - Max agent-loop iterations per role
# ============================================================

package kb.proxy

import rego.v1

# ── Provider Access ──────────────────────────────────────────
# Which agent roles may use the proxy to access LLM providers.

default provider_allowed := false

provider_allowed if {
    input.agent_role in {"analyst", "developer", "admin"}
}

# ── Required Tools ───────────────────────────────────────────
# MCP tools that MUST be injected into every LLM request.
# The proxy merges these into the tools[] array transparently.

default required_tools := {"search_knowledge", "check_policy"}

# ── Max Iterations ───────────────────────────────────────────
# Maximum agent-loop iterations (tool-call cycles) per role.
# Prevents runaway loops.

default max_iterations := 5

max_iterations := 10 if {
    input.agent_role in {"developer", "admin"}
}
```

**Step 4: Run tests to verify they pass**

Run: `docker exec kb-opa /opa test /policies/kb/ -v --run 'proxy'`
Expected: All 7 tests PASS

**Step 5: Run full OPA test suite to verify no regressions**

Run: `docker exec kb-opa /opa test /policies/kb/ -v`
Expected: All tests PASS (proxy + summarization + any existing tests)

**Step 6: Commit**

```bash
git add opa-policies/kb/proxy.rego opa-policies/kb/test_proxy.rego
git commit -m "feat(opa): add proxy policies for AI provider proxy

Controls provider access by role, required tools for injection,
and max agent-loop iterations."
```

---

### Task 2: Proxy Service — Configuration

**Files:**
- Create: `pb-proxy/config.py`
- Create: `pb-proxy/requirements.txt`

**Step 1: Create requirements.txt**

Create `pb-proxy/requirements.txt`:

```
fastapi>=0.115
uvicorn[standard]>=0.34
litellm>=1.60
mcp>=1.0
httpx>=0.27
pydantic>=2.0
prometheus-client>=0.21
tenacity>=9.0
```

**Step 2: Create config module**

Create `pb-proxy/config.py`:

```python
"""
pb-proxy configuration.
Reads from environment variables with sensible defaults.
Supports Docker Secrets via _FILE suffix convention.
"""

import os
import logging

log = logging.getLogger("pb-proxy")


def _read_secret(env_var: str, default: str = "") -> str:
    """Read from Docker Secret file if available, else fall back to env var."""
    file_path = os.getenv(f"{env_var}_FILE")
    if file_path:
        try:
            return open(file_path).read().strip()
        except FileNotFoundError:
            log.warning("Secret file %s not found, falling back to env var", file_path)
    return os.getenv(env_var, default)


# ── Service ──────────────────────────────────────────────────
PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8090"))

# ── MCP Server ───────────────────────────────────────────────
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8080/mcp")

# ── OPA ──────────────────────────────────────────────────────
OPA_URL = os.getenv("OPA_URL", "http://opa:8181")

# ── LiteLLM ──────────────────────────────────────────────────
LITELLM_CONFIG = os.getenv("LITELLM_CONFIG", "/app/litellm_config.yaml")

# ── Agent Loop ───────────────────────────────────────────────
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "10"))
TOOL_CALL_TIMEOUT = int(os.getenv("TOOL_CALL_TIMEOUT", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

# ── Tool Injection ───────────────────────────────────────────
TOOL_REFRESH_INTERVAL = int(os.getenv("TOOL_REFRESH_INTERVAL", "60"))

# ── Failure Mode ─────────────────────────────────────────────
# "closed" = return 503 if MCP server unreachable
# "open" = forward request without tool injection
FAIL_MODE = os.getenv("FAIL_MODE", "closed")

# ── Metrics ──────────────────────────────────────────────────
METRICS_PORT = int(os.getenv("METRICS_PORT", "9092"))
```

**Step 3: Commit**

```bash
git add pb-proxy/config.py pb-proxy/requirements.txt
git commit -m "feat(proxy): add configuration and dependencies"
```

---

### Task 3: Proxy Service — Tool Injection

**Files:**
- Create: `pb-proxy/tests/__init__.py`
- Create: `pb-proxy/tests/test_tool_injection.py`
- Create: `pb-proxy/tool_injection.py`

**Step 1: Write the failing test**

Create `pb-proxy/tests/__init__.py` (empty).

Create `pb-proxy/tests/test_tool_injection.py`:

```python
"""Tests for MCP tool discovery and OpenAI schema conversion."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tool_injection import ToolInjector, mcp_tool_to_openai


# ── Schema conversion ────────────────────────────────────────

def test_mcp_tool_to_openai_converts_correctly():
    """MCP Tool schema converts to OpenAI function-calling format."""
    mcp_tool = MagicMock()
    mcp_tool.name = "search_knowledge"
    mcp_tool.description = "Semantic search"
    mcp_tool.inputSchema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }

    result = mcp_tool_to_openai(mcp_tool)

    assert result == {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Semantic search",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    }


# ── Tool merge ───────────────────────────────────────────────

def test_merge_tools_injects_powerbrain_tools():
    """Powerbrain tools are added to client tools."""
    injector = ToolInjector.__new__(ToolInjector)
    injector._openai_tools = {
        "search_knowledge": {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Search",
                "parameters": {"type": "object"},
            },
        }
    }

    client_tools = [
        {
            "type": "function",
            "function": {"name": "my_custom_tool", "description": "Custom"},
        }
    ]

    merged = injector.merge_tools(client_tools)

    names = [t["function"]["name"] for t in merged]
    assert "search_knowledge" in names
    assert "my_custom_tool" in names
    assert len(merged) == 2


def test_merge_tools_powerbrain_wins_on_conflict():
    """When client sends a tool with same name as Powerbrain tool, Powerbrain wins."""
    injector = ToolInjector.__new__(ToolInjector)
    injector._openai_tools = {
        "search_knowledge": {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Powerbrain version",
                "parameters": {"type": "object"},
            },
        }
    }

    client_tools = [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Client override attempt",
                "parameters": {},
            },
        }
    ]

    merged = injector.merge_tools(client_tools)

    assert len(merged) == 1
    assert merged[0]["function"]["description"] == "Powerbrain version"


def test_merge_tools_empty_client_tools():
    """When client sends no tools, only Powerbrain tools are returned."""
    injector = ToolInjector.__new__(ToolInjector)
    injector._openai_tools = {
        "search_knowledge": {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Search",
                "parameters": {"type": "object"},
            },
        }
    }

    merged = injector.merge_tools(None)
    assert len(merged) == 1

    merged2 = injector.merge_tools([])
    assert len(merged2) == 1
```

**Step 2: Run test to verify it fails**

Run: `cd pb-proxy && python -m pytest tests/test_tool_injection.py -v`
Expected: FAIL (module not found)

**Step 3: Write implementation**

Create `pb-proxy/tool_injection.py`:

```python
"""
Tool injection: discovers Powerbrain MCP tools and merges them
into OpenAI-compatible tool arrays.
"""

import asyncio
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import config

log = logging.getLogger("pb-proxy.tools")


def mcp_tool_to_openai(tool: Any) -> dict:
    """Convert an MCP Tool object to OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema or {"type": "object"},
        },
    }


class ToolInjector:
    """Discovers tools from the Powerbrain MCP server and injects them."""

    def __init__(self) -> None:
        self._mcp_tools: dict[str, Any] = {}       # name → MCP Tool object
        self._openai_tools: dict[str, dict] = {}    # name → OpenAI schema
        self._refresh_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Initial tool discovery and start periodic refresh."""
        await self._refresh_tools()
        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        log.info("ToolInjector started, %d tools discovered", len(self._mcp_tools))

    async def stop(self) -> None:
        """Stop periodic refresh."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _refresh_tools(self) -> None:
        """Connect to MCP server and fetch current tool list."""
        try:
            async with streamablehttp_client(config.MCP_SERVER_URL) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    new_mcp: dict[str, Any] = {}
                    new_openai: dict[str, dict] = {}
                    for tool in result.tools:
                        new_mcp[tool.name] = tool
                        new_openai[tool.name] = mcp_tool_to_openai(tool)
                    self._mcp_tools = new_mcp
                    self._openai_tools = new_openai
                    log.debug("Refreshed %d tools from MCP server", len(new_mcp))
        except Exception as e:
            if not self._mcp_tools:
                log.error("Failed to discover tools (no cache): %s", e)
                raise
            log.warning("Tool refresh failed, using cached tools (%d): %s",
                        len(self._mcp_tools), e)

    async def _periodic_refresh(self) -> None:
        """Periodically refresh tool list."""
        while True:
            await asyncio.sleep(config.TOOL_REFRESH_INTERVAL)
            try:
                await self._refresh_tools()
            except Exception as e:
                log.warning("Periodic tool refresh failed: %s", e)

    def merge_tools(self, client_tools: list[dict] | None) -> list[dict]:
        """Merge Powerbrain tools into client's tool array.

        - Powerbrain tools are always included.
        - Client tools with same name as Powerbrain tools are replaced.
        - Client tools with unique names are preserved.
        """
        result: dict[str, dict] = {}

        # Add Powerbrain tools first (they take precedence)
        result.update(self._openai_tools)

        # Add client tools (skip conflicts — Powerbrain wins)
        if client_tools:
            for tool in client_tools:
                name = tool.get("function", {}).get("name", "")
                if name and name not in result:
                    result[name] = tool

        return list(result.values())

    @property
    def tool_names(self) -> set[str]:
        """Names of all currently known Powerbrain tools."""
        return set(self._mcp_tools.keys())

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Execute a tool call against the MCP server. Returns result as string."""
        async with streamablehttp_client(config.MCP_SERVER_URL) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                # Concatenate all text content
                texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        texts.append(content.text)
                return "\n".join(texts) if texts else str(result.content)
```

**Step 4: Run test to verify it passes**

Run: `cd pb-proxy && python -m pytest tests/test_tool_injection.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add pb-proxy/tool_injection.py pb-proxy/tests/
git commit -m "feat(proxy): add tool injection with MCP discovery and merge logic"
```

---

### Task 4: Proxy Service — Agent Loop

**Files:**
- Create: `pb-proxy/tests/test_agent_loop.py`
- Create: `pb-proxy/agent_loop.py`

**Step 1: Write the failing test**

Create `pb-proxy/tests/test_agent_loop.py`:

```python
"""Tests for the agent loop (tool-call execution cycle)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent_loop import AgentLoop, AgentLoopResult


@pytest.fixture
def mock_tool_injector():
    injector = MagicMock()
    injector.tool_names = {"search_knowledge", "check_policy"}
    injector.call_tool = AsyncMock(return_value='{"results": []}')
    return injector


@pytest.mark.asyncio
async def test_no_tool_calls_returns_immediately(mock_tool_injector):
    """When LLM responds without tool calls, return immediately."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.tool_calls = None
    mock_response.choices[0].finish_reason = "stop"
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)

    with patch("agent_loop.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        loop = AgentLoop(mock_tool_injector, max_iterations=5)
        result = await loop.run(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Hello"}],
            tools=[],
        )

    assert result.response == mock_response
    assert result.iterations == 1
    assert result.tool_calls_executed == 0


@pytest.mark.asyncio
async def test_tool_call_is_executed(mock_tool_injector):
    """When LLM responds with a Powerbrain tool call, execute it."""
    # First call: LLM returns tool_call
    tool_call = MagicMock()
    tool_call.id = "call_123"
    tool_call.function.name = "search_knowledge"
    tool_call.function.arguments = '{"query": "test"}'

    first_response = MagicMock()
    first_response.choices = [MagicMock()]
    first_response.choices[0].message.tool_calls = [tool_call]
    first_response.choices[0].message.role = "assistant"
    first_response.choices[0].message.content = None
    first_response.choices[0].finish_reason = "tool_calls"

    # Second call: LLM returns final response
    second_response = MagicMock()
    second_response.choices = [MagicMock()]
    second_response.choices[0].message.tool_calls = None
    second_response.choices[0].finish_reason = "stop"
    second_response.usage = MagicMock(prompt_tokens=50, completion_tokens=30, total_tokens=80)

    with patch("agent_loop.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(
            side_effect=[first_response, second_response]
        )

        loop = AgentLoop(mock_tool_injector, max_iterations=5)
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
    tool_call = MagicMock()
    tool_call.id = "call_loop"
    tool_call.function.name = "search_knowledge"
    tool_call.function.arguments = '{"query": "loop"}'

    loop_response = MagicMock()
    loop_response.choices = [MagicMock()]
    loop_response.choices[0].message.tool_calls = [tool_call]
    loop_response.choices[0].message.role = "assistant"
    loop_response.choices[0].message.content = None
    loop_response.choices[0].finish_reason = "tool_calls"
    loop_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10, total_tokens=20)

    with patch("agent_loop.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=loop_response)

        loop = AgentLoop(mock_tool_injector, max_iterations=3)
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
    tool_call = MagicMock()
    tool_call.id = "call_unknown"
    tool_call.function.name = "unknown_tool"
    tool_call.function.arguments = "{}"

    first_response = MagicMock()
    first_response.choices = [MagicMock()]
    first_response.choices[0].message.tool_calls = [tool_call]
    first_response.choices[0].message.role = "assistant"
    first_response.choices[0].message.content = None
    first_response.choices[0].finish_reason = "tool_calls"

    second_response = MagicMock()
    second_response.choices = [MagicMock()]
    second_response.choices[0].message.tool_calls = None
    second_response.choices[0].finish_reason = "stop"
    second_response.usage = MagicMock(prompt_tokens=30, completion_tokens=20, total_tokens=50)

    with patch("agent_loop.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(
            side_effect=[first_response, second_response]
        )

        loop = AgentLoop(mock_tool_injector, max_iterations=5)
        result = await loop.run(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Use unknown"}],
            tools=[],
        )

    # Tool was not called on the injector (it's unknown)
    mock_tool_injector.call_tool.assert_not_called()
    assert result.iterations == 2
```

**Step 2: Run test to verify it fails**

Run: `cd pb-proxy && python -m pytest tests/test_agent_loop.py -v`
Expected: FAIL (module not found)

**Step 3: Write implementation**

Create `pb-proxy/agent_loop.py`:

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

import litellm
from tool_injection import ToolInjector

log = logging.getLogger("pb-proxy.loop")


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

    def __init__(self, tool_injector: ToolInjector, max_iterations: int = 10) -> None:
        self._injector = tool_injector
        self._max_iterations = max_iterations

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

            # Call LLM
            response = await litellm.acompletion(
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
                    arguments = {}

                if tool_name in self._injector.tool_names:
                    # Execute against MCP server
                    try:
                        tool_result = await asyncio.wait_for(
                            self._injector.call_tool(tool_name, arguments),
                            timeout=30,
                        )
                        result.tool_calls_executed += 1
                        result.tools_used.append(tool_name)
                    except asyncio.TimeoutError:
                        tool_result = json.dumps({
                            "error": f"Tool '{tool_name}' timed out after 30s"
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

**Step 4: Run test to verify it passes**

Run: `cd pb-proxy && python -m pytest tests/test_agent_loop.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add pb-proxy/agent_loop.py pb-proxy/tests/test_agent_loop.py
git commit -m "feat(proxy): add agent loop for tool-call execution cycle"
```

---

### Task 5: Proxy Service — Main Application

**Files:**
- Create: `pb-proxy/tests/test_proxy.py`
- Create: `pb-proxy/proxy.py`

**Step 1: Write the failing test**

Create `pb-proxy/tests/test_proxy.py`:

```python
"""Tests for the main proxy FastAPI application."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_deps():
    """Mock all external dependencies."""
    with patch("proxy.tool_injector") as mock_injector, \
         patch("proxy.check_opa_policy") as mock_opa, \
         patch("proxy.AgentLoop") as mock_loop_cls:

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
        }


@pytest.fixture
def client(mock_deps):
    from proxy import app
    return TestClient(app)


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_chat_completions_requires_model(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )
    assert response.status_code == 422  # Validation error — model required


def test_chat_completions_success(client, mock_deps):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 200


def test_chat_completions_with_client_tools(client, mock_deps):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Use my tool"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "my_tool",
                        "description": "A custom tool",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        },
    )
    assert response.status_code == 200
    # Verify merge_tools was called with client tools
    mock_deps["injector"].merge_tools.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `cd pb-proxy && python -m pytest tests/test_proxy.py -v`
Expected: FAIL (module not found)

**Step 3: Write implementation**

Create `pb-proxy/proxy.py`:

```python
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
```

**Step 4: Run test to verify it passes**

Run: `cd pb-proxy && python -m pytest tests/test_proxy.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/tests/test_proxy.py
git commit -m "feat(proxy): add main FastAPI application with /v1/chat/completions"
```

---

### Task 6: Dockerfile and Docker Compose Integration

**Files:**
- Create: `pb-proxy/Dockerfile`
- Create: `pb-proxy/litellm_config.yaml`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Step 1: Create Dockerfile**

Create `pb-proxy/Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

EXPOSE 8090

CMD ["python", "proxy.py"]
```

**Step 2: Create default LiteLLM config**

Create `pb-proxy/litellm_config.yaml`:

```yaml
# LiteLLM Model Configuration for Powerbrain AI Provider Proxy
# Docs: https://docs.litellm.ai/docs/proxy/configs
#
# Configure your LLM providers here. Each entry maps a model_name
# (used in API requests) to a provider-specific model + credentials.
#
# Examples:
#
# model_list:
#   - model_name: "gpt-4o"
#     litellm_params:
#       model: "openai/gpt-4o"
#       api_key: "os.environ/OPENAI_API_KEY"
#
#   - model_name: "claude-sonnet"
#     litellm_params:
#       model: "anthropic/claude-sonnet-4-20250514"
#       api_key: "os.environ/ANTHROPIC_API_KEY"
#
#   - model_name: "local-llama"
#     litellm_params:
#       model: "ollama/llama3.2"
#       api_base: "http://ollama:11434"

model_list: []
```

**Step 3: Add proxy service to docker-compose.yml**

Add the following before the Caddy service block in `docker-compose.yml`
(after line 318, before the Caddy comment on line 320):

```yaml
  # ── AI Provider Proxy (optional) ──────────────────────────
  pb-proxy:
    profiles: ["proxy"]
    build:
      context: ./pb-proxy
      dockerfile: Dockerfile
    container_name: kb-proxy
    ports:
      - "${PROXY_PORT:-8090}:8090"
    environment:
      MCP_SERVER_URL:          http://mcp-server:8080/mcp
      OPA_URL:                 http://opa:8181
      LITELLM_CONFIG:          /app/litellm_config.yaml
      PROXY_HOST:              "0.0.0.0"
      PROXY_PORT:              "8090"
      TOOL_REFRESH_INTERVAL:   ${TOOL_REFRESH_INTERVAL:-60}
      MAX_ITERATIONS:          ${MAX_ITERATIONS:-10}
      TOOL_CALL_TIMEOUT:       ${TOOL_CALL_TIMEOUT:-30}
      REQUEST_TIMEOUT:         ${REQUEST_TIMEOUT:-120}
      FAIL_MODE:               ${FAIL_MODE:-closed}
      METRICS_PORT:            "9092"
    volumes:
      - ./pb-proxy/litellm_config.yaml:/app/litellm_config.yaml:ro
    depends_on:
      mcp-server:
        condition: service_started
      opa:
        condition: service_healthy
    networks:
      - kb-net
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8090/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

**Step 4: Add proxy variables to .env.example**

Append to `.env.example`:

```bash
# ── AI Provider Proxy (optional, docker compose --profile proxy) ──
# PROXY_PORT=8090
# TOOL_REFRESH_INTERVAL=60
# MAX_ITERATIONS=10
# TOOL_CALL_TIMEOUT=30
# REQUEST_TIMEOUT=120
# FAIL_MODE=closed
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
```

**Step 5: Verify Docker Compose config is valid**

Run: `docker compose config --profiles proxy 2>&1 | head -20`
Expected: Valid YAML output, no errors

**Step 6: Commit**

```bash
git add pb-proxy/Dockerfile pb-proxy/litellm_config.yaml docker-compose.yml .env.example
git commit -m "feat(proxy): add Dockerfile and Docker Compose profile

Activated via: docker compose --profile proxy up -d
Consistent with existing profile pattern (tls, seed)."
```

---

### Task 7: Identity Updates — README, what-is-powerbrain, CLAUDE.md

**Files:**
- Modify: `README.md`
- Modify: `docs/what-is-powerbrain.md`
- Modify: `CLAUDE.md`

**Step 1: Update README.md**

Add the 7th core feature after the "Self-Hosted" feature block (after line 48,
before "Quick Start"):

```markdown
🔀 **AI Provider Proxy** — Optional gateway between your AI consumers and their LLM providers. Transparently injects Powerbrain tools into every LLM request and executes tool calls automatically. Your teams use any LLM they prefer (100+ providers via LiteLLM); Powerbrain ensures they always query policy-checked enterprise context. Activate with `docker compose --profile proxy up`.
```

Update the architecture diagram in README.md to show the proxy as an optional
path (replace the existing diagram between lines 17-34).

Add proxy activation to Quick Start section after line 83:

```markdown
### Optional: AI Provider Proxy

```bash
# Configure your LLM provider(s) in pb-proxy/litellm_config.yaml
docker compose --profile proxy up -d

# Now use OpenAI-compatible API with automatic Powerbrain tools:
curl http://localhost:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"What are our GDPR deletion policies?"}]}'
```
```

**Step 2: Update docs/what-is-powerbrain.md**

Add 7th feature section after "Self-Hosted & GDPR-Native" (after line 80):

```markdown
### 🔀 AI Provider Proxy

Optional gateway that sits between AI consumers and LLM providers. Injects Powerbrain tools transparently into every request, executes tool calls automatically, and returns the final response. Supports 100+ LLM providers via [LiteLLM](https://github.com/BerriAI/litellm).

Two access patterns:
1. **Direct MCP** — Agent speaks MCP natively (existing, standard)
2. **Via Proxy** — Agent speaks OpenAI-compatible API, proxy handles MCP transparently

Activate with `docker compose --profile proxy up`. OPA policies control which tools are mandatory, which providers are allowed, and iteration limits.
```

Update the "How is This Different?" table to add a row:

```markdown
| **DIY proxy / gateway** | No MCP awareness, no tool enforcement | Transparent tool injection with policy control |
```

**Step 3: Update CLAUDE.md**

Add pb-proxy to the Components table:

```markdown
| pb-proxy      | 8090  | Python, FastAPI, LiteLLM, MCP SDK    | AI Provider Proxy (optional)     |
```

Add to Completed Features:

```markdown
9. ✅ **AI Provider Proxy** — Optional LLM gateway with transparent tool injection (`docker compose --profile proxy`)
```

Add to Key Concepts:

```markdown
### AI Provider Proxy (optional)
Optional gateway activated via `docker compose --profile proxy up`.
Sits between AI consumers and LLM providers:
1. Client sends OpenAI-compatible request to proxy (port 8090)
2. Proxy injects Powerbrain MCP tools into `tools[]` array
3. Forwards augmented request to LLM (via LiteLLM, 100+ providers)
4. When LLM returns tool calls → proxy executes against MCP server
5. Repeats until final response, then returns to client

OPA policies (`kb.proxy`) control: provider access, required tools, max iterations.
Configuration: `pb-proxy/litellm_config.yaml` for LLM provider setup.
```

Add to the Directory Structure:

```markdown
├── pb-proxy/
│   ├── proxy.py           ← Main FastAPI application
│   ├── tool_injection.py  ← MCP tool discovery + merge
│   ├── agent_loop.py      ← Tool-call execution loop
│   ├── config.py          ← Configuration
│   ├── litellm_config.yaml← LLM provider config
│   ├── Dockerfile
│   └── requirements.txt
```

**Step 4: Commit**

```bash
git add README.md docs/what-is-powerbrain.md CLAUDE.md
git commit -m "docs: add AI Provider Proxy as 7th core feature

Update identity across README, what-is-powerbrain, and CLAUDE.md.
New supporting claim: 'Bring your own LLM. Keep our guardrails.'"
```

---

### Task 8: Deployment Documentation

**Files:**
- Modify: `docs/deployment.md`

**Step 1: Add proxy section to deployment.md**

Add a new section "AI Provider Proxy" after the Docker Secrets section:

```markdown
## AI Provider Proxy

The proxy is an optional service that intercepts LLM API requests and
injects Powerbrain tools transparently.

### Setup

1. **Configure LLM providers** in `pb-proxy/litellm_config.yaml`:

```yaml
model_list:
  - model_name: "gpt-4o"
    litellm_params:
      model: "openai/gpt-4o"
      api_key: "os.environ/OPENAI_API_KEY"
```

2. **Set API keys** in `.env`:

```bash
OPENAI_API_KEY=sk-...
```

3. **Start with proxy profile:**

```bash
docker compose --profile proxy up -d
```

4. **Verify:**

```bash
curl http://localhost:8090/health
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_PORT` | `8090` | Proxy port |
| `TOOL_REFRESH_INTERVAL` | `60` | Seconds between MCP tool refresh |
| `MAX_ITERATIONS` | `10` | Default max agent-loop iterations |
| `TOOL_CALL_TIMEOUT` | `30` | Timeout per tool call (seconds) |
| `REQUEST_TIMEOUT` | `120` | Total request timeout (seconds) |
| `FAIL_MODE` | `closed` | `open` or `closed` when MCP unreachable |

### Usage

Send requests using OpenAI-compatible format:

```bash
curl http://localhost:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "What are our data retention policies?"}]
  }'
```

The proxy automatically injects Powerbrain tools. If the LLM decides to
use `search_knowledge` or any other Powerbrain tool, the proxy executes
the call against the MCP server and feeds the result back to the LLM.

### Combining profiles

```bash
# Proxy + TLS
docker compose --profile proxy --profile tls up -d

# Proxy + seed data
docker compose --profile proxy --profile seed up -d
```
```

**Step 2: Commit**

```bash
git add docs/deployment.md
git commit -m "docs: add proxy deployment guide"
```

---

### Task 9: Integration Test

**Files:**
- Create: `pb-proxy/tests/test_integration.py`

**Step 1: Write integration test**

Create `pb-proxy/tests/test_integration.py`:

```python
"""
Integration test for the proxy.
Requires: mcp-server, opa running (skip if not available).
"""

import os
import pytest
import httpx

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8090")
MCP_URL = os.getenv("MCP_URL", "http://localhost:8080")


def is_service_running(url: str) -> bool:
    try:
        resp = httpx.get(f"{url}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(
    not is_service_running(os.getenv("PROXY_URL", "http://localhost:8090")),
    reason="Proxy service not running",
)
class TestProxyIntegration:

    def test_health(self):
        resp = httpx.get(f"{PROXY_URL}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["tools_loaded"] > 0

    def test_chat_completions_without_api_key(self):
        """Request without LLM API key should fail gracefully."""
        resp = httpx.post(
            f"{PROXY_URL}/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            timeout=30,
        )
        # Should fail with 502 (LLM provider error) not 500 (server crash)
        assert resp.status_code in (502, 403)
```

**Step 2: Commit**

```bash
git add pb-proxy/tests/test_integration.py
git commit -m "test(proxy): add integration test skeleton"
```

---

### Task 10: Update Identity Design Document

**Files:**
- Modify: `docs/plans/2026-03-21-identity-hardening-design.md`

**Step 1: Update core features list**

In `docs/plans/2026-03-21-identity-hardening-design.md`, update the
"Core features" list (around line 26-32) to include the 7th feature:

```markdown
**Core features (identity-defining, to be hardened):**
1. Policy-aware Context Delivery (OPA)
2. Sealed Vault & Pseudonymization
3. Relevance Pipeline (Oversampling → Policy → Reranking)
4. Context Summarization (policy-controlled)
5. MCP-native Interface
6. Self-hosted / GDPR-native
7. AI Provider Proxy (transparent tool enforcement) — NEW
```

Add supporting claim after line 16:

```markdown
- *"Bring your own LLM. Keep our guardrails."* — for proxy / provider-agnostic contexts
```

**Step 2: Commit**

```bash
git add docs/plans/2026-03-21-identity-hardening-design.md
git commit -m "docs: add AI Provider Proxy to identity core features"
```

---

## Backlog (not in this plan)

These items are documented in the design doc
(`docs/plans/2026-03-21-ai-proxy-kb-api-design.md`, Section 5-6)
and deferred to future sprints:

- **KB REST API** — Separate service for human users
- **Multi-MCP-Server** — Proxy aggregates tools from N MCP servers
- **SSE streaming** — Streaming passthrough with tool-call interception
- **Client tool passthrough** — Forward unknown tool calls to client
- **Proxy auth** — API key or OAuth2 authentication for proxy consumers
