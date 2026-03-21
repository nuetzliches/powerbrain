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
