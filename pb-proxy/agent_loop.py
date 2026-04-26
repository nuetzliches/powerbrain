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
from pii_middleware import (
    depseudonymize_tool_arguments,
    pseudonymize_tool_result,
    vault_resolve_tool_result,
)
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
    # Aggregate of /vault/resolve stats across all tool results in the loop.
    # Surfaces in the proxy response's `_telemetry` block so clients (e.g.
    # the sales-demo UI) can show "resolved 3 of 7 pseudonyms" counters.
    vault_resolutions: dict[str, int] = field(
        default_factory=lambda: {"total": 0, "resolved": 0, "skipped": 0}
    )


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
        pii_http_client: Any | None = None,
        pii_session_salt: str | None = None,
        user_token: str | None = None,
        client_headers: dict[str, str] | None = None,
        # Enterprise-tier vault resolution of tool-result pseudonyms.
        # All four are required together; missing mcp_url/token disables
        # resolution even when `vault_resolve_enabled=True`.
        vault_resolve_enabled: bool = False,
        vault_resolve_purpose: str | None = None,
        vault_resolve_mcp_url: str | None = None,
        vault_resolve_mcp_token: str | None = None,
    ) -> None:
        self._injector = tool_injector
        self._acompletion = acompletion
        self._max_iterations = max_iterations
        self._tool_call_timeout = tool_call_timeout or config.TOOL_CALL_TIMEOUT
        self._pii_reverse_map = pii_reverse_map or {}
        self._pii_http_client = pii_http_client
        self._pii_session_salt = pii_session_salt
        self._user_token = user_token
        self._client_headers = client_headers
        self._vault_resolve_enabled = bool(
            vault_resolve_enabled
            and vault_resolve_purpose
            and vault_resolve_mcp_url
            and vault_resolve_mcp_token
            and pii_http_client
        )
        self._vault_resolve_purpose = vault_resolve_purpose or ""
        self._vault_resolve_mcp_url = vault_resolve_mcp_url or ""
        self._vault_resolve_mcp_token = vault_resolve_mcp_token or ""

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

                # Pseudonymize tool result if the tool's contract requires it
                if (entry and entry.needs_pii_scan
                        and self._pii_http_client and self._pii_session_salt):
                    try:
                        tool_result = await pseudonymize_tool_result(
                            tool_result,
                            self._pii_session_salt,
                            self._pii_http_client,
                            self._pii_reverse_map,
                        )
                    except Exception as e:
                        log.warning("PII scan of tool result failed (continuing): %s", e)

                # Enterprise: ask mcp-server /vault/resolve to replace
                # pseudonyms the proxy's own session never saw (knowledge-
                # base-side pseudonyms) with vault-resolved originals.
                # Runs AFTER pseudonymize so resolved values stay intact
                # (pseudonymize only extends reverse_map, never rewrites
                # tokens that don't look like PII).
                if self._vault_resolve_enabled:
                    try:
                        tool_result, vr_stats = await vault_resolve_tool_result(
                            tool_result,
                            purpose=self._vault_resolve_purpose,
                            mcp_url=self._vault_resolve_mcp_url,
                            mcp_token=self._vault_resolve_mcp_token,
                            http_client=self._pii_http_client,
                        )
                        for k in ("total", "resolved", "skipped"):
                            result.vault_resolutions[k] += vr_stats.get(k, 0)
                    except Exception as e:
                        log.warning(
                            "Vault resolution of tool result failed "
                            "(continuing with pseudonyms): %s", e
                        )

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
