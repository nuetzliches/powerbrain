"""
MCP server configuration: loads server definitions from YAML
with fallback to legacy MCP_SERVER_URL env var.
"""

import logging
from dataclasses import dataclass

import yaml

import config

log = logging.getLogger("pb-proxy.mcp-config")


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server."""
    name: str
    url: str
    auth: str = "none"              # "bearer", "static", "none"
    auth_token_env: str | None = None  # env var name for static auth
    prefix: str = ""                # tool name prefix
    required: bool = False          # fail-fast if unreachable
    tool_whitelist: list[str] | None = None  # None = all tools


def load_mcp_servers(config_path: str) -> list[McpServerConfig]:
    """Load MCP server config from YAML or fall back to env var.

    Fallback: If the YAML file doesn't exist, creates a single-server
    config from the legacy MCP_SERVER_URL env var.
    """
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.info(
            "MCP servers config not found at %s, falling back to MCP_SERVER_URL",
            config_path,
        )
        return [
            McpServerConfig(
                name="powerbrain",
                url=config.MCP_SERVER_URL,
                auth="bearer",
                prefix="powerbrain",
                required=True,
            )
        ]

    servers_data = data.get("servers", [])
    if not servers_data:
        raise ValueError("MCP servers config has empty 'servers' list")

    servers = []
    for entry in servers_data:
        servers.append(McpServerConfig(
            name=entry["name"],
            url=entry["url"],
            auth=entry.get("auth", "none"),
            auth_token_env=entry.get("auth_token_env"),
            prefix=entry.get("prefix", entry["name"]),
            required=entry.get("required", False),
            tool_whitelist=entry.get("tool_whitelist"),
        ))

    # Validate: no duplicate names or prefixes
    names = [s.name for s in servers]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate server name in MCP config: {names}")
    prefixes = [s.prefix for s in servers]
    if len(prefixes) != len(set(prefixes)):
        raise ValueError(f"Duplicate prefix in MCP config: {prefixes}")

    log.info("Loaded %d MCP server(s): %s", len(servers), [s.name for s in servers])
    return servers
