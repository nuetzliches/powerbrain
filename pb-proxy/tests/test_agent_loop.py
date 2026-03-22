"""Tests for the agent loop (tool-call execution cycle)."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_loop import AgentLoop, AgentLoopResult


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


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def mock_tool_injector():
    injector = MagicMock()
    injector.tool_names = {"search_knowledge", "check_policy"}
    injector.call_tool = AsyncMock(return_value='{"results": []}')
    # resolve_tool_name returns the name if known, None otherwise
    def _resolve(name):
        known = {"search_knowledge", "check_policy"}
        if name in known:
            return name
        # Strip prefix: "anything_toolname" → "toolname"
        if "_" in name:
            suffix = name.split("_", 1)[1]
            if suffix in known:
                return suffix
        return None
    injector.resolve_tool_name = MagicMock(side_effect=_resolve)
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
        "search_knowledge", {"query": "test"}
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
