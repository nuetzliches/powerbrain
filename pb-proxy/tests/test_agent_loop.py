"""Tests for the agent loop (tool-call execution cycle)."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_loop import AgentLoop, AgentLoopResult
from tool_injection import ToolEntry
from mcp_config import McpServerConfig


# ── Helpers ──────────────────────────────────────────────────


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
    """Helper to create a mock tool call object."""
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


# ── Shared server config for test ToolEntry objects ──────────

_DEFAULT_SERVER_CONFIG = McpServerConfig(
    name="powerbrain",
    url="http://mcp:8080/mcp",
    auth="bearer",
    prefix="powerbrain",
    required=True,
)

_SEARCH_ENTRY = ToolEntry(
    server_name="powerbrain",
    original_name="search_knowledge",
    schema={},
    server_config=_DEFAULT_SERVER_CONFIG,
)

_POLICY_ENTRY = ToolEntry(
    server_name="powerbrain",
    original_name="check_policy",
    schema={},
    server_config=_DEFAULT_SERVER_CONFIG,
)

# Map of prefixed names to ToolEntry objects
_TOOL_ENTRIES = {
    "search_knowledge": _SEARCH_ENTRY,
    "powerbrain_search_knowledge": _SEARCH_ENTRY,
    "check_policy": _POLICY_ENTRY,
    "powerbrain_check_policy": _POLICY_ENTRY,
}


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def mock_tool_injector():
    injector = MagicMock()
    injector.tool_names = {"powerbrain_search_knowledge", "powerbrain_check_policy"}
    injector.call_tool = AsyncMock(return_value='{"results": []}')
    # resolve_tool returns ToolEntry if known, None otherwise
    def _resolve(name):
        return _TOOL_ENTRIES.get(name)
    injector.resolve_tool = MagicMock(side_effect=_resolve)
    return injector


# ── Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_tool_calls_returns_immediately(mock_tool_injector):
    """When LLM responds without tool calls, return immediately."""
    mock_response = _make_response()
    mock_acompletion = AsyncMock(return_value=mock_response)

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
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
    tool_call = _make_tool_call("call_123", "search_knowledge", '{"query": "test"}')
    first_response = _make_response(tool_calls=[tool_call], finish_reason="tool_calls")
    second_response = _make_response()

    mock_acompletion = AsyncMock(side_effect=[first_response, second_response])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Search for test"}],
        tools=[],
    )

    assert result.iterations == 2
    assert result.tool_calls_executed == 1
    mock_tool_injector.call_tool.assert_called_once_with(
        _SEARCH_ENTRY, {"query": "test"}, user_token=None,
    )


@pytest.mark.asyncio
async def test_max_iterations_stops_loop(mock_tool_injector):
    """Loop stops after max_iterations and returns last response."""
    tool_call = _make_tool_call("call_loop", "search_knowledge", '{"query": "loop"}')
    loop_response = _make_response(tool_calls=[tool_call], finish_reason="tool_calls")

    mock_acompletion = AsyncMock(return_value=loop_response)

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
    tool_call = _make_tool_call("call_unknown", "unknown_tool")
    first_response = _make_response(tool_calls=[tool_call], finish_reason="tool_calls")
    second_response = _make_response()

    mock_acompletion = AsyncMock(side_effect=[first_response, second_response])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Use unknown"}],
        tools=[],
    )

    # Tool was not called on the injector (it's unknown)
    mock_tool_injector.call_tool.assert_not_called()
    assert result.iterations == 2


@pytest.mark.asyncio
async def test_tool_call_timeout_feeds_error_to_llm(mock_tool_injector):
    """Tool timeout returns error JSON to LLM, loop continues."""
    mock_tool_injector.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())

    tool_call = _make_tool_call("call_timeout", "search_knowledge", '{"query": "slow"}')
    first_response = _make_response(tool_calls=[tool_call], finish_reason="tool_calls")
    second_response = _make_response()

    mock_acompletion = AsyncMock(side_effect=[first_response, second_response])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Slow query"}],
        tools=[],
    )

    assert result.iterations == 2
    # Tool call was attempted but timed out — not counted as executed
    assert result.tool_calls_executed == 0


@pytest.mark.asyncio
async def test_tool_call_exception_feeds_error_to_llm(mock_tool_injector):
    """Tool exception returns error JSON to LLM, loop continues."""
    mock_tool_injector.call_tool = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )

    tool_call = _make_tool_call("call_error", "search_knowledge", '{"query": "broken"}')
    first_response = _make_response(tool_calls=[tool_call], finish_reason="tool_calls")
    second_response = _make_response()

    mock_acompletion = AsyncMock(side_effect=[first_response, second_response])

    loop = AgentLoop(mock_tool_injector, acompletion=mock_acompletion, max_iterations=5)
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Broken query"}],
        tools=[],
    )

    assert result.iterations == 2
    # Tool call failed — not counted as executed
    assert result.tool_calls_executed == 0


@pytest.mark.asyncio
async def test_agent_loop_routes_to_correct_server():
    """Tool calls are routed to the correct MCP server via ToolEntry."""
    # Setup mock injector
    injector = MagicMock()
    pb_entry = ToolEntry(
        server_name="powerbrain",
        original_name="search_knowledge",
        schema={},
        server_config=McpServerConfig(
            name="powerbrain", url="http://mcp:8080/mcp",
            auth="bearer", prefix="powerbrain", required=True,
        ),
    )
    injector.resolve_tool.return_value = pb_entry
    injector.call_tool = AsyncMock(return_value='{"results": []}')

    # Mock LLM: first call returns tool_call, second returns final response
    tool_call = _make_tool_call(
        "call_1",
        "powerbrain_search_knowledge",
        '{"query": "test"}',
    )
    llm_call_1 = _make_response(tool_calls=[tool_call], finish_reason="tool_calls")
    llm_call_2 = _make_response()
    mock_acompletion = AsyncMock(side_effect=[llm_call_1, llm_call_2])

    loop = AgentLoop(
        injector,
        acompletion=mock_acompletion,
        max_iterations=5,
        user_token="pb_test_token_123",
    )
    result = await loop.run(
        model="gpt-4o",
        messages=[{"role": "user", "content": "search for test"}],
        tools=[],
    )

    # Verify tool was called with entry and user_token
    injector.call_tool.assert_called_once_with(
        pb_entry, {"query": "test"}, user_token="pb_test_token_123",
    )
    assert result.tool_calls_executed == 1
    assert result.tools_used == ["search_knowledge"]
