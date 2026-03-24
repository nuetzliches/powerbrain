"""Tests for multi-server ToolInjector."""

import os
from unittest.mock import patch

from mcp_config import McpServerConfig


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


def test_resolve_tool_with_prefix():
    """resolve_tool looks up ToolEntry by prefixed name."""
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


# ── _mcp_headers auth logic tests ────────────────────────────


def test_mcp_headers_bearer_with_user_token():
    """Bearer auth uses user_token when provided."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(name="s", url="http://s:8080/mcp", auth="bearer")
    headers = _mcp_headers(server, user_token="kb_user_key_123")
    assert headers["Authorization"] == "Bearer kb_user_key_123"


def test_mcp_headers_bearer_fallback():
    """Bearer auth falls back to config.MCP_AUTH_TOKEN when no user_token."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(name="s", url="http://s:8080/mcp", auth="bearer")
    with patch("config.MCP_AUTH_TOKEN", "admin-token"):
        headers = _mcp_headers(server, user_token=None)
    assert headers["Authorization"] == "Bearer admin-token"


def test_mcp_headers_static_from_env():
    """Static auth reads token from env var."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(
        name="s", url="http://s:8080/mcp",
        auth="static", auth_token_env="TEST_MCP_TOKEN",
    )
    with patch.dict(os.environ, {"TEST_MCP_TOKEN": "static-secret"}):
        headers = _mcp_headers(server)
    assert headers["Authorization"] == "Bearer static-secret"


def test_mcp_headers_none():
    """Auth mode 'none' produces no headers."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(name="s", url="http://s:8080/mcp", auth="none")
    headers = _mcp_headers(server)
    assert headers == {}
