"""Tests for MCP server configuration loading."""

import pytest
import tempfile
import os
from pathlib import Path


def test_load_config_from_yaml():
    """Load MCP server config from YAML file."""
    from mcp_config import load_mcp_servers, McpServerConfig

    yaml_content = """
servers:
  - name: powerbrain
    url: http://mcp-server:8080/mcp
    auth: bearer
    prefix: powerbrain
    required: true
  - name: github
    url: http://github-mcp:3000/mcp
    auth: static
    auth_token_env: GITHUB_MCP_TOKEN
    prefix: github
    required: false
    tool_whitelist:
      - list_repos
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        servers = load_mcp_servers(f.name)

    os.unlink(f.name)

    assert len(servers) == 2
    assert servers[0].name == "powerbrain"
    assert servers[0].auth == "bearer"
    assert servers[0].required is True
    assert servers[0].tool_whitelist is None

    assert servers[1].name == "github"
    assert servers[1].auth == "static"
    assert servers[1].auth_token_env == "GITHUB_MCP_TOKEN"
    assert servers[1].required is False
    assert servers[1].tool_whitelist == ["list_repos"]


def test_fallback_to_env_var():
    """When no YAML exists, fall back to MCP_SERVER_URL env var."""
    from mcp_config import load_mcp_servers

    servers = load_mcp_servers("/nonexistent/path.yaml")

    assert len(servers) == 1
    assert servers[0].name == "powerbrain"
    assert servers[0].auth == "bearer"
    assert servers[0].required is True


def test_duplicate_prefix_raises():
    """Duplicate prefixes in config raise ValueError."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: server1
    url: http://s1:8080/mcp
    prefix: same
  - name: server2
    url: http://s2:8080/mcp
    prefix: same
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        with pytest.raises(ValueError, match="Duplicate prefix"):
            load_mcp_servers(f.name)
    os.unlink(f.name)


def test_duplicate_name_raises():
    """Duplicate server names in config raise ValueError."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: same
    url: http://s1:8080/mcp
    prefix: prefix1
  - name: same
    url: http://s2:8080/mcp
    prefix: prefix2
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        with pytest.raises(ValueError, match="Duplicate server name"):
            load_mcp_servers(f.name)
    os.unlink(f.name)
