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


def _mcp_headers() -> dict[str, str]:
    """Build auth headers for MCP server connection."""
    headers: dict[str, str] = {}
    if config.MCP_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {config.MCP_AUTH_TOKEN}"
    return headers


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
        self._mcp_tools: dict[str, Any] = {}       # name -> MCP Tool object
        self._openai_tools: dict[str, dict] = {}    # name -> OpenAI schema
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
            async with streamablehttp_client(
                config.MCP_SERVER_URL,
                headers=_mcp_headers(),
            ) as (
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

        # Add client tools (skip conflicts - Powerbrain wins)
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

    def resolve_tool_name(self, name: str) -> str | None:
        """Resolve a tool name, stripping common prefixes added by MCP clients.

        Clients like OpenCode prefix MCP tool names with the server name
        (e.g. ``powerbrain_search_knowledge`` for ``search_knowledge``).
        This method tries the exact name first, then strips known prefixes.

        Returns the canonical tool name or None if not found.
        """
        if name in self._mcp_tools:
            return name
        # Strip prefix: "anything_toolname" → "toolname"
        if "_" in name:
            suffix = name.split("_", 1)[1]
            if suffix in self._mcp_tools:
                return suffix
        return None

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Execute a tool call against the MCP server. Returns result as string."""
        async with streamablehttp_client(
            config.MCP_SERVER_URL,
            headers=_mcp_headers(),
        ) as (
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
