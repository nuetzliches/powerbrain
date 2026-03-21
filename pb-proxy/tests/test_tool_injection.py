"""Tests for MCP tool discovery and OpenAI schema conversion."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

# Add parent directory to path so we can import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tool_injection import ToolInjector, mcp_tool_to_openai


# ── Schema conversion ────────────────────────────────────────

def test_mcp_tool_to_openai_converts_correctly():
    """MCP Tool schema converts to OpenAI function-calling format."""
    mcp_tool = MagicMock()
    mcp_tool.name = "search_knowledge"
    mcp_tool.description = "Semantic search"
    mcp_tool.inputSchema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }

    result = mcp_tool_to_openai(mcp_tool)

    assert result == {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Semantic search",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    }


# ── Tool merge ───────────────────────────────────────────────

def test_merge_tools_injects_powerbrain_tools():
    """Powerbrain tools are added to client tools."""
    injector = ToolInjector.__new__(ToolInjector)
    injector._openai_tools = {
        "search_knowledge": {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Search",
                "parameters": {"type": "object"},
            },
        }
    }

    client_tools = [
        {
            "type": "function",
            "function": {"name": "my_custom_tool", "description": "Custom"},
        }
    ]

    merged = injector.merge_tools(client_tools)

    names = [t["function"]["name"] for t in merged]
    assert "search_knowledge" in names
    assert "my_custom_tool" in names
    assert len(merged) == 2


def test_merge_tools_powerbrain_wins_on_conflict():
    """When client sends a tool with same name as Powerbrain tool, Powerbrain wins."""
    injector = ToolInjector.__new__(ToolInjector)
    injector._openai_tools = {
        "search_knowledge": {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Powerbrain version",
                "parameters": {"type": "object"},
            },
        }
    }

    client_tools = [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Client override attempt",
                "parameters": {},
            },
        }
    ]

    merged = injector.merge_tools(client_tools)

    assert len(merged) == 1
    assert merged[0]["function"]["description"] == "Powerbrain version"


def test_merge_tools_empty_client_tools():
    """When client sends no tools, only Powerbrain tools are returned."""
    injector = ToolInjector.__new__(ToolInjector)
    injector._openai_tools = {
        "search_knowledge": {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Search",
                "parameters": {"type": "object"},
            },
        }
    }

    merged = injector.merge_tools(None)
    assert len(merged) == 1

    merged2 = injector.merge_tools([])
    assert len(merged2) == 1
