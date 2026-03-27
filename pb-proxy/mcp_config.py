"""
MCP server configuration: loads server definitions from YAML
with fallback to legacy MCP_SERVER_URL env var.
"""

import logging
from dataclasses import dataclass

import yaml

import config

log = logging.getLogger("pb-proxy.mcp-config")


_VALID_AUTH_MODES = {"bearer", "static", "none"}


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server."""
    name: str
    url: str
    auth: str = "none"              # "bearer", "static", "none"
    auth_token_env: str | None = None  # env var name for static auth
    prefix: str | None = None       # tool name prefix (defaults to name)
    required: bool = False          # fail-fast if unreachable
    tool_whitelist: list[str] | None = None  # None = all tools
    forward_headers: list[str] | None = None  # headers to forward from client request

    def __post_init__(self) -> None:
        if self.prefix is None:
            self.prefix = self.name


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
    for i, entry in enumerate(servers_data):
        if "name" not in entry or "url" not in entry:
            missing = [f for f in ("name", "url") if f not in entry]
            raise ValueError(
                f"Server entry #{i + 1} missing required field(s): {missing}"
            )
        url = entry["url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"Server '{entry['name']}': url must start with http:// or https://, got '{url}'"
            )
        auth = entry.get("auth", "none")
        if auth not in _VALID_AUTH_MODES:
            raise ValueError(
                f"Server '{entry['name']}': auth must be one of {_VALID_AUTH_MODES}, got '{auth}'"
            )
        servers.append(McpServerConfig(
            name=entry["name"],
            url=url,
            auth=auth,
            auth_token_env=entry.get("auth_token_env"),
            prefix=entry.get("prefix"),
            required=entry.get("required", False),
            tool_whitelist=entry.get("tool_whitelist"),
            forward_headers=entry.get("forward_headers"),
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
