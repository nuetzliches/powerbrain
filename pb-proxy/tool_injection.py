"""
Multi-MCP-server tool injection: discovers tools from N MCP servers,
prefixes them with server name, and routes tool calls to the correct server.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import config
from mcp_config import McpServerConfig, load_mcp_servers

log = logging.getLogger("pb-proxy.tools")


@dataclass
class ToolEntry:
    """A discovered tool with routing metadata."""
    server_name: str
    original_name: str
    schema: dict              # OpenAI function-calling format
    server_config: McpServerConfig


def _mcp_headers(server: McpServerConfig, user_token: str | None = None) -> dict[str, str]:
    """Build auth headers for an MCP server connection.

    Args:
        server: The MCP server config.
        user_token: The user's pb_ API key (for bearer auth mode).
    """
    headers: dict[str, str] = {}
    if server.auth == "bearer":
        token = user_token or config.MCP_AUTH_TOKEN
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif server.auth == "static":
        if server.auth_token_env:
            token = os.getenv(server.auth_token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
    # auth == "none": no headers
    return headers


def _mcp_tool_to_openai(tool: Any, prefix: str) -> dict:
    """Convert an MCP Tool to OpenAI function-calling format with prefix."""
    prefixed_name = f"{prefix}_{tool.name}" if prefix else tool.name
    return {
        "type": "function",
        "function": {
            "name": prefixed_name,
            "description": tool.description or "",
            "parameters": tool.inputSchema or {"type": "object"},
        },
    }


class ToolInjector:
    """Discovers tools from multiple MCP servers and injects them into LLM requests."""

    def __init__(self) -> None:
        self._servers: list[McpServerConfig] = []
        self._tools: dict[str, ToolEntry] = {}   # prefixed_name -> ToolEntry
        self._refresh_task: asyncio.Task | None = None
        self._server_status: dict[str, bool] = {}  # server_name -> reachable

    async def start(self) -> None:
        """Load config, discover tools from all servers, start periodic refresh."""
        self._servers = load_mcp_servers(config.MCP_SERVERS_CONFIG)

        # Initial discovery
        await self._refresh_all_tools()

        # Check: all required servers must have tools
        for server in self._servers:
            if server.required and not self._server_status.get(server.name, False):
                raise RuntimeError(
                    f"Required MCP server '{server.name}' ({server.url}) is unreachable"
                )

        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        log.info(
            "ToolInjector started: %d server(s), %d tool(s)",
            len(self._servers), len(self._tools),
        )

    async def stop(self) -> None:
        """Stop periodic refresh."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _refresh_all_tools(self) -> None:
        """Refresh tools from all configured servers."""
        new_tools: dict[str, ToolEntry] = {}

        for server in self._servers:
            try:
                server_tools = await self._discover_server_tools(server)
                for prefixed_name, entry in server_tools.items():
                    new_tools[prefixed_name] = entry
                self._server_status[server.name] = True
                log.debug(
                    "Server '%s': %d tool(s)", server.name, len(server_tools),
                )
            except Exception as e:
                self._server_status[server.name] = False
                if server.required:
                    log.error("Required server '%s' unreachable: %s", server.name, e)
                else:
                    log.warning("Optional server '%s' unreachable: %s", server.name, e)

        if new_tools:
            self._tools = new_tools
        elif self._tools:
            log.warning("All servers unreachable, keeping cached tools (%d)", len(self._tools))
        else:
            raise RuntimeError("No MCP servers reachable and no cached tools")

    async def _discover_server_tools(
        self, server: McpServerConfig,
    ) -> dict[str, ToolEntry]:
        """Connect to one MCP server and discover its tools."""
        headers = _mcp_headers(server)
        tools: dict[str, ToolEntry] = {}

        async with streamablehttp_client(server.url, headers=headers) as (
            read_stream, write_stream, _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()

                for tool in result.tools:
                    # Apply whitelist filter
                    if server.tool_whitelist and tool.name not in server.tool_whitelist:
                        continue
                    prefixed_name = f"{server.prefix}_{tool.name}" if server.prefix else tool.name
                    tools[prefixed_name] = ToolEntry(
                        server_name=server.name,
                        original_name=tool.name,
                        schema=_mcp_tool_to_openai(tool, server.prefix),
                        server_config=server,
                    )

        return tools

    async def _periodic_refresh(self) -> None:
        """Periodically refresh tool list from all servers."""
        while True:
            await asyncio.sleep(config.TOOL_REFRESH_INTERVAL)
            try:
                await self._refresh_all_tools()
            except Exception as e:
                log.warning("Periodic tool refresh failed: %s", e)

    def merge_tools(
        self,
        client_tools: list[dict] | None,
        allowed_servers: list[str] | None = None,
    ) -> list[dict]:
        """Merge MCP tools into client's tool array.

        Args:
            client_tools: Client-provided tools (preserved if unique names).
            allowed_servers: If set, only include tools from these servers.
                             If None, include all.
        """
        result: dict[str, dict] = {}

        # Add MCP tools (filtered by allowed_servers)
        for name, entry in self._tools.items():
            if allowed_servers is not None and entry.server_name not in allowed_servers:
                continue
            result[name] = entry.schema

        # Add client tools (skip conflicts — MCP tools win)
        if client_tools:
            for tool in client_tools:
                name = tool.get("function", {}).get("name", "")
                if name and name not in result:
                    result[name] = tool

        return list(result.values())

    @property
    def tool_names(self) -> set[str]:
        """Names of all currently known tools (prefixed)."""
        return set(self._tools.keys())

    @property
    def server_names(self) -> list[str]:
        """Names of all configured servers."""
        return [s.name for s in self._servers]

    def resolve_tool(self, name: str) -> ToolEntry | None:
        """Look up a tool by its prefixed name. Returns ToolEntry or None."""
        return self._tools.get(name)

    async def call_tool(
        self,
        entry: ToolEntry,
        arguments: dict,
        user_token: str | None = None,
    ) -> str:
        """Execute a tool call against the correct MCP server.

        Args:
            entry: The ToolEntry (from resolve_tool).
            arguments: Tool arguments dict.
            user_token: User's pb_ key for bearer auth propagation.
        """
        headers = _mcp_headers(entry.server_config, user_token=user_token)

        async with streamablehttp_client(
            entry.server_config.url, headers=headers,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(entry.original_name, arguments)
                texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        texts.append(content.text)
                return "\n".join(texts) if texts else str(result.content)
