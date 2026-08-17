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


# ── prefix application tests ─────────────────────────────────


def test_prefixed_tool_name_applies_prefix():
    """A tool that is not namespaced gets the server prefix."""
    from tool_injection import _prefixed_tool_name

    assert _prefixed_tool_name("powerbrain", "search_knowledge") == "powerbrain_search_knowledge"
    assert _prefixed_tool_name("tc", "list_timesheets") == "tc_list_timesheets"


def test_prefixed_tool_name_is_idempotent():
    """A tool already named `<prefix>_…` is not prefixed twice.

    timecockpit-mcp publishes `tc_list_timesheets` etc., so the `prefix: tc`
    entry must not produce `tc_tc_list_timesheets`.
    """
    from tool_injection import _prefixed_tool_name

    assert _prefixed_tool_name("tc", "tc_list_timesheets") == "tc_list_timesheets"
    assert _prefixed_tool_name("tc", "tc_find_similar_entries") == "tc_find_similar_entries"


def test_prefixed_tool_name_partial_match_still_prefixed():
    """A name that merely starts with the prefix letters still gets prefixed."""
    from tool_injection import _prefixed_tool_name

    # "tcfoo" starts with "tc" but is not namespaced ("tc_"), so it needs the prefix.
    assert _prefixed_tool_name("tc", "tcfoo") == "tc_tcfoo"


def test_prefixed_tool_name_no_prefix():
    """An empty or missing prefix leaves the name untouched."""
    from tool_injection import _prefixed_tool_name

    assert _prefixed_tool_name("", "search_knowledge") == "search_knowledge"
    assert _prefixed_tool_name(None, "search_knowledge") == "search_knowledge"


def test_mcp_tool_to_openai_uses_idempotent_prefix():
    """Schema name and lookup key agree for an already-namespaced tool."""
    from tool_injection import _mcp_tool_to_openai

    class _Tool:
        name = "tc_create_timesheet"
        description = "Create a timesheet entry"
        input_schema = {"type": "object", "properties": {}}

    schema = _mcp_tool_to_openai(_Tool(), "tc")
    assert schema["function"]["name"] == "tc_create_timesheet"


# ── _mcp_headers auth logic tests ────────────────────────────


def test_mcp_headers_bearer_with_user_token():
    """Bearer auth uses user_token when provided."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(name="s", url="http://s:8080/mcp", auth="bearer")
    headers = _mcp_headers(server, user_token="pb_user_key_123")
    assert headers["Authorization"] == "Bearer pb_user_key_123"


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


# ── forward_headers tests ────────────────────────────────────


def test_mcp_headers_forwards_configured_headers():
    """forward_headers picks matching headers from client request."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(
        name="s", url="http://s:8080/mcp", auth="none",
        forward_headers=["x-custom-token", "x-tenant-id"],
    )
    client_headers = {
        "x-custom-token": "secret-123",
        "x-tenant-id": "tenant-42",
        "authorization": "Bearer pb_key",
        "host": "localhost",
    }
    headers = _mcp_headers(server, client_headers=client_headers)
    assert headers == {
        "x-custom-token": "secret-123",
        "x-tenant-id": "tenant-42",
    }


def test_mcp_headers_forward_missing_header_is_skipped():
    """Missing client headers are silently skipped (no crash)."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(
        name="s", url="http://s:8080/mcp", auth="none",
        forward_headers=["x-custom-token", "x-missing"],
    )
    client_headers = {"x-custom-token": "val"}
    headers = _mcp_headers(server, client_headers=client_headers)
    assert headers == {"x-custom-token": "val"}


def test_mcp_headers_forward_none_means_no_forwarding():
    """forward_headers=None (default) forwards nothing."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(name="s", url="http://s:8080/mcp", auth="none")
    client_headers = {"x-custom-token": "val", "x-tenant-id": "t1"}
    headers = _mcp_headers(server, client_headers=client_headers)
    assert headers == {}


def test_mcp_headers_forward_no_client_headers():
    """forward_headers set but client_headers is None — no crash."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(
        name="s", url="http://s:8080/mcp", auth="none",
        forward_headers=["x-custom-token"],
    )
    headers = _mcp_headers(server, client_headers=None)
    assert headers == {}


def test_mcp_headers_forward_combined_with_bearer_auth():
    """forward_headers works alongside bearer auth headers."""
    from tool_injection import _mcp_headers

    server = McpServerConfig(
        name="s", url="http://s:8080/mcp", auth="bearer",
        forward_headers=["x-tenant-id"],
    )
    client_headers = {"x-tenant-id": "tenant-42"}
    headers = _mcp_headers(server, user_token="pb_key_123", client_headers=client_headers)
    assert headers == {
        "Authorization": "Bearer pb_key_123",
        "x-tenant-id": "tenant-42",
    }


# ── ExceptionGroup flattening for discovery failures ─────────


def test_leaf_exceptions_plain_exception_is_its_own_leaf():
    """A non-group exception flattens to just itself."""
    from tool_injection import _leaf_exceptions

    exc = ValueError("boom")
    assert _leaf_exceptions(exc) == [exc]


def test_leaf_exceptions_flattens_nested_groups():
    """Groups nest, so flattening recurses to the real leaves."""
    from tool_injection import _leaf_exceptions

    inner_a = ConnectionRefusedError("connect refused")
    inner_b = TimeoutError("read timeout")
    outer = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ExceptionGroup("inner group", [inner_a, inner_b])],
    )
    assert _leaf_exceptions(outer) == [inner_a, inner_b]


def test_describe_exception_plain_exception_names_type():
    """A plain exception is rendered as `Type: message`."""
    from tool_injection import _describe_exception

    assert _describe_exception(ValueError("bad url")) == "ValueError: bad url"


def test_describe_exception_surfaces_taskgroup_leaf_cause():
    """The leaf cause replaces the TaskGroup group's own useless str().

    The MCP streamable-http client wraps failures in an anyio task group, so
    logging the group with %s used to yield only "unhandled errors in a
    TaskGroup (1 sub-exception)" with no HTTP status or auth error.
    """
    from tool_injection import _describe_exception

    class HTTPStatusError(Exception):
        pass

    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [HTTPStatusError("Client error '401 Unauthorized' for url 'http://tc:8080/mcp'")],
    )

    # What the old `%s` formatting produced — the bug being fixed.
    assert "TaskGroup" in str(group)
    assert "401" not in str(group)

    described = _describe_exception(group)
    assert described == (
        "HTTPStatusError: Client error '401 Unauthorized' for url 'http://tc:8080/mcp'"
    )
    assert "TaskGroup" not in described


def test_describe_exception_joins_multiple_leaves():
    """Several sub-exceptions are all reported, not just the first."""
    from tool_injection import _describe_exception

    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ConnectionRefusedError("connect refused"), TimeoutError("read timeout")],
    )
    assert _describe_exception(group) == (
        "ConnectionRefusedError: connect refused; TimeoutError: read timeout"
    )


def test_describe_exception_collapses_multiline_messages():
    """A multi-line leaf message is collapsed to keep the log one line.

    httpx raises `Client error '401 Unauthorized' for url '…'\\nFor more
    information check: …`, which would otherwise wrap the warning.
    """
    from tool_injection import _describe_exception

    class HTTPStatusError(Exception):
        pass

    leaf = HTTPStatusError(
        "Client error '401 Unauthorized' for url 'http://tc:8080/mcp'\n"
        "For more information check: https://developer.mozilla.org/…/401"
    )
    described = _describe_exception(ExceptionGroup("unhandled errors in a TaskGroup", [leaf]))
    assert "\n" not in described
    assert described == (
        "HTTPStatusError: Client error '401 Unauthorized' for url 'http://tc:8080/mcp' "
        "For more information check: https://developer.mozilla.org/…/401"
    )


def test_describe_exception_message_less_leaf_falls_back_to_type():
    """An exception with an empty str() still names its type."""
    from tool_injection import _describe_exception

    class ConnectError(Exception):
        pass

    assert _describe_exception(ConnectError()) == "ConnectError"
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ConnectError()])
    assert _describe_exception(group) == "ConnectError"


def _cached_tool_entry():
    """A single cached ToolEntry, so refresh keeps cache instead of raising."""
    from tool_injection import ToolEntry

    return ToolEntry(
        server_name="powerbrain",
        original_name="search_knowledge",
        schema={},
        server_config=McpServerConfig(
            name="powerbrain", url="http://mcp:8080/mcp",
            auth="bearer", prefix="powerbrain", required=True,
        ),
    )


async def test_refresh_logs_leaf_cause_for_optional_server(caplog):
    """The optional-server warning names the leaf cause, not the TaskGroup."""
    import logging

    from tool_injection import ToolInjector

    server = McpServerConfig(
        name="timecockpit", url="http://tc:8080/mcp", auth="static",
        prefix="tc", required=False,
    )
    injector = ToolInjector.__new__(ToolInjector)
    injector._servers = [server]
    injector._server_status = {}
    injector._tools = {"powerbrain_search_knowledge": _cached_tool_entry()}

    async def _boom(_self, _server):
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [PermissionError("401 Unauthorized")],
        )

    with patch.object(ToolInjector, "_discover_server_tools", _boom):
        with caplog.at_level(logging.DEBUG, logger="pb-proxy.tools"):
            await injector._refresh_all_tools()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    unreachable = [r for r in warnings if "timecockpit" in r.getMessage()]
    assert len(unreachable) == 1
    message = unreachable[0].getMessage()
    assert "PermissionError: 401 Unauthorized" in message
    assert "TaskGroup" not in message

    # Traceback is still available, but only at debug level.
    debug_with_traceback = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and r.exc_info is not None
    ]
    assert debug_with_traceback, "expected a debug record carrying the traceback"

    assert injector._server_status["timecockpit"] is False


async def test_refresh_logs_leaf_cause_for_required_server(caplog):
    """The required-server error line gets the same flattening."""
    import logging

    from tool_injection import ToolInjector

    server = McpServerConfig(
        name="powerbrain", url="http://mcp:8080/mcp", auth="bearer",
        prefix="powerbrain", required=True,
    )
    injector = ToolInjector.__new__(ToolInjector)
    injector._servers = [server]
    injector._server_status = {}
    injector._tools = {"powerbrain_search_knowledge": _cached_tool_entry()}

    async def _boom(_self, _server):
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ExceptionGroup("inner", [ConnectionRefusedError("connect refused")])],
        )

    with patch.object(ToolInjector, "_discover_server_tools", _boom):
        with caplog.at_level(logging.DEBUG, logger="pb-proxy.tools"):
            await injector._refresh_all_tools()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "ConnectionRefusedError: connect refused" in message
    assert "TaskGroup" not in message
