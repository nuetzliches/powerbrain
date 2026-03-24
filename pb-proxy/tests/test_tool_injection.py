"""Tests for multi-server ToolInjector."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from mcp_config import McpServerConfig


@dataclass
class FakeTool:
    name: str
    description: str = "A test tool"
    inputSchema: dict | None = None


@pytest.fixture
def two_server_config():
    """Two MCP server configs."""
    return [
        McpServerConfig(
            name="powerbrain", url="http://mcp:8080/mcp",
            auth="bearer", prefix="powerbrain", required=True,
        ),
        McpServerConfig(
            name="github", url="http://github:3000/mcp",
            auth="static", auth_token_env="GITHUB_TOKEN",
            prefix="github", required=False,
        ),
    ]


def test_tool_entry_from_prefix():
    """ToolEntry stores server info and original name."""
    from tool_injection import ToolEntry

    entry = ToolEntry(
        server_name="powerbrain",
        original_name="search_knowledge",
        schema={"type": "function", "function": {"name": "powerbrain_search_knowledge"}},
        server_config=McpServerConfig(
            name="powerbrain", url="http://mcp:8080/mcp",
            auth="bearer", prefix="powerbrain", required=True,
        ),
    )
    assert entry.server_name == "powerbrain"
    assert entry.original_name == "search_knowledge"


def test_resolve_tool_name_with_prefix():
    """resolve_tool strips prefix and returns server + original name."""
    from tool_injection import ToolInjector, ToolEntry

    injector = ToolInjector.__new__(ToolInjector)
    injector._tools = {
        "powerbrain_search_knowledge": ToolEntry(
            server_name="powerbrain",
            original_name="search_knowledge",
            schema={},
            server_config=McpServerConfig(
                name="powerbrain", url="http://mcp:8080/mcp",
                auth="bearer", prefix="powerbrain", required=True,
            ),
        ),
        "github_list_repos": ToolEntry(
            server_name="github",
            original_name="list_repos",
            schema={},
            server_config=McpServerConfig(
                name="github", url="http://github:3000/mcp",
                auth="static", prefix="github", required=False,
            ),
        ),
    }

    entry = injector.resolve_tool("powerbrain_search_knowledge")
    assert entry is not None
    assert entry.server_name == "powerbrain"
    assert entry.original_name == "search_knowledge"

    entry = injector.resolve_tool("github_list_repos")
    assert entry is not None
    assert entry.server_name == "github"

    entry = injector.resolve_tool("unknown_tool")
    assert entry is None


def test_merge_tools_includes_all_servers():
    """merge_tools includes tools from all servers with prefixed names."""
    from tool_injection import ToolInjector, ToolEntry

    injector = ToolInjector.__new__(ToolInjector)
    injector._tools = {
        "powerbrain_search": ToolEntry(
            server_name="powerbrain", original_name="search",
            schema={"type": "function", "function": {"name": "powerbrain_search", "description": "Search", "parameters": {}}},
            server_config=McpServerConfig(name="powerbrain", url="u", prefix="powerbrain"),
        ),
        "github_list": ToolEntry(
            server_name="github", original_name="list",
            schema={"type": "function", "function": {"name": "github_list", "description": "List", "parameters": {}}},
            server_config=McpServerConfig(name="github", url="u", prefix="github"),
        ),
    }

    merged = injector.merge_tools(None)
    names = {t["function"]["name"] for t in merged}
    assert "powerbrain_search" in names
    assert "github_list" in names


def test_merge_tools_filters_by_allowed_servers():
    """merge_tools with allowed_servers filter only includes allowed tools."""
    from tool_injection import ToolInjector, ToolEntry

    injector = ToolInjector.__new__(ToolInjector)
    injector._tools = {
        "powerbrain_search": ToolEntry(
            server_name="powerbrain", original_name="search",
            schema={"type": "function", "function": {"name": "powerbrain_search", "description": "Search", "parameters": {}}},
            server_config=McpServerConfig(name="powerbrain", url="u", prefix="powerbrain"),
        ),
        "github_list": ToolEntry(
            server_name="github", original_name="list",
            schema={"type": "function", "function": {"name": "github_list", "description": "List", "parameters": {}}},
            server_config=McpServerConfig(name="github", url="u", prefix="github"),
        ),
    }

    merged = injector.merge_tools(None, allowed_servers=["powerbrain"])
    names = {t["function"]["name"] for t in merged}
    assert "powerbrain_search" in names
    assert "github_list" not in names
