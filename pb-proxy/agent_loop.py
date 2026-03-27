"""
Agent loop: executes tool calls from LLM responses against
MCP servers (routed via ToolInjector) and re-submits results
until the LLM produces a final response.
"""

import json
import logging
import asyncio
import sys
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from tool_injection import ToolInjector
from pii_middleware import depseudonymize_tool_arguments
import config

# Try to import telemetry - fallback if not available (for tests)
try:
    shared_path = os.path.join(os.path.dirname(__file__), "..", "shared")
    if shared_path not in sys.path:
        sys.path.insert(0, shared_path)
    from shared.telemetry import trace_operation, get_current_telemetry
except ImportError:
    # Mock for tests
    from contextlib import nullcontext
    def trace_operation(*args, **kwargs):
        return nullcontext()
    def get_current_telemetry():
        return None

log = logging.getLogger("pb-proxy.loop")

# Type alias for the async completion callable
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
    """Executes the tool-call loop between LLM and MCP servers."""

    def __init__(
        self,
        tool_injector: ToolInjector,
        *,
        acompletion: ACompletion,
        max_iterations: int = 10,
        tool_call_timeout: int | None = None,
        pii_reverse_map: dict[str, str] | None = None,
        user_token: str | None = None,
        client_headers: dict[str, str] | None = None,
    ) -> None:
        self._injector = tool_injector
        self._acompletion = acompletion
        self._max_iterations = max_iterations
        self._tool_call_timeout = tool_call_timeout or config.TOOL_CALL_TIMEOUT
        self._pii_reverse_map = pii_reverse_map or {}
        self._user_token = user_token
        self._client_headers = client_headers

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
            with trace_operation(None, "llm_call", "pb-proxy",
                               model=model, iteration=iteration):
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

                # De-pseudonymize tool arguments before MCP call
                if self._pii_reverse_map:
                    arguments = depseudonymize_tool_arguments(
                        arguments, self._pii_reverse_map
                    )

                # Resolve tool: look up ToolEntry by prefixed name
                entry = self._injector.resolve_tool(tool_name)

                if entry:
                    log.info("Routing tool '%s' → server '%s', tool '%s'",
                             tool_name, entry.server_name, entry.original_name)
                    
                    with trace_operation(None, "tool_dispatch", "pb-proxy",
                                       tool_name=tool_name):
                        try:
                            tool_result = await asyncio.wait_for(
                                self._injector.call_tool(
                                    entry, arguments,
                                    user_token=self._user_token,
                                    client_headers=self._client_headers,
                                ),
                                timeout=self._tool_call_timeout,
                            )
                            result.tool_calls_executed += 1
                            result.tools_used.append(entry.original_name)
                        except asyncio.TimeoutError:
                            tool_result = json.dumps({
                                "error": f"Tool '{entry.original_name}' on server "
                                         f"'{entry.server_name}' timed out after "
                                         f"{self._tool_call_timeout}s"
                            })
                            log.warning("Tool call timed out: %s/%s",
                                        entry.server_name, entry.original_name)
                        except Exception as e:
                            tool_result = json.dumps({
                                "error": f"Tool '{entry.original_name}' on server "
                                         f"'{entry.server_name}' failed: {str(e)}"
                            })
                            log.error("Tool call failed: %s/%s — %s",
                                      entry.server_name, entry.original_name, e)
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
