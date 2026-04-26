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


def test_forward_headers_loaded_from_yaml():
    """forward_headers field is loaded from YAML config."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: external
    url: http://ext:8080/mcp
    auth: none
    forward_headers:
      - x-tenant-id
      - x-custom-token
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        servers = load_mcp_servers(f.name)

    os.unlink(f.name)

    assert len(servers) == 1
    assert servers[0].forward_headers == ["x-tenant-id", "x-custom-token"]


def test_forward_headers_defaults_to_none():
    """forward_headers defaults to None when not specified in YAML."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: basic
    url: http://basic:8080/mcp
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        servers = load_mcp_servers(f.name)

    os.unlink(f.name)

    assert servers[0].forward_headers is None


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


# ── PII Contract Tests ─────────────────────────────────────


def test_pii_status_loaded_from_yaml():
    """pii_status and pii_scanned_tools are loaded from YAML config."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: powerbrain
    url: http://mcp:8080/mcp
    pii_status: scanned
  - name: crm
    url: http://crm:3000/mcp
    prefix: crm
    pii_status: mixed
    pii_scanned_tools:
      - crm_find_similar_records
      - crm_analyze_patterns
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        servers = load_mcp_servers(f.name)

    os.unlink(f.name)

    assert servers[0].pii_status == "scanned"
    assert servers[0].pii_scanned_tools is None

    assert servers[1].pii_status == "mixed"
    assert servers[1].pii_scanned_tools == ["crm_find_similar_records", "crm_analyze_patterns"]


def test_pii_status_defaults_to_unscanned():
    """Missing pii_status defaults to 'unscanned' (fail-safe)."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: basic
    url: http://basic:8080/mcp
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        servers = load_mcp_servers(f.name)

    os.unlink(f.name)

    assert servers[0].pii_status == "unscanned"
    assert servers[0].pii_scanned_tools is None


def test_invalid_pii_status_raises():
    """Invalid pii_status value raises ValueError."""
    from mcp_config import load_mcp_servers

    yaml_content = """
servers:
  - name: bad
    url: http://bad:8080/mcp
    pii_status: invalid_value
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        with pytest.raises(ValueError, match="pii_status"):
            load_mcp_servers(f.name)
    os.unlink(f.name)


def test_tool_entry_needs_pii_scan():
    """ToolEntry.needs_pii_scan reflects server's pii_status contract."""
    from mcp_config import McpServerConfig
    from tool_injection import ToolEntry

    # scanned server → no scan needed
    scanned_config = McpServerConfig(name="pb", url="http://pb:8080/mcp", pii_status="scanned")
    entry = ToolEntry(server_name="pb", original_name="search_knowledge", schema={}, server_config=scanned_config)
    assert entry.needs_pii_scan is False

    # unscanned server → scan needed
    unscanned_config = McpServerConfig(name="ext", url="http://ext:8080/mcp", pii_status="unscanned")
    entry = ToolEntry(server_name="ext", original_name="list_data", schema={}, server_config=unscanned_config)
    assert entry.needs_pii_scan is True

    # mixed server, tool in scanned list → no scan
    mixed_config = McpServerConfig(
        name="crm", url="http://crm:3000/mcp", pii_status="mixed",
        pii_scanned_tools=["crm_find_similar_records", "crm_analyze_patterns"],
    )
    entry = ToolEntry(server_name="crm", original_name="crm_find_similar_records", schema={}, server_config=mixed_config)
    assert entry.needs_pii_scan is False

    # mixed server, tool NOT in scanned list → scan needed
    entry = ToolEntry(server_name="crm", original_name="crm_list_contacts", schema={}, server_config=mixed_config)
    assert entry.needs_pii_scan is True

    # mixed server, empty scanned list → all need scan
    mixed_empty = McpServerConfig(name="crm2", url="http://crm:3000/mcp", pii_status="mixed")
    entry = ToolEntry(server_name="crm2", original_name="any_tool", schema={}, server_config=mixed_empty)
    assert entry.needs_pii_scan is True
