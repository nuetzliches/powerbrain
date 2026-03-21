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
