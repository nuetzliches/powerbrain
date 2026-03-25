"""
pb-proxy configuration.
Reads from environment variables with sensible defaults.
Supports Docker Secrets via _FILE suffix convention.
"""

import os
import logging

log = logging.getLogger("pb-proxy")


def _read_secret(env_var: str, default: str = "") -> str:
    """Read from Docker Secret file if available, else fall back to env var."""
    file_path = os.getenv(f"{env_var}_FILE")
    if file_path:
        try:
            return open(file_path).read().strip()
        except FileNotFoundError:
            log.warning("Secret file %s not found, falling back to env var", file_path)
    return os.getenv(env_var, default)


# ── Service ──────────────────────────────────────────────────
PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8090"))

# ── MCP Server ───────────────────────────────────────────────
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8080/mcp")
MCP_AUTH_TOKEN = _read_secret("MCP_AUTH_TOKEN", "")

# ── OPA ──────────────────────────────────────────────────────
OPA_URL = os.getenv("OPA_URL", "http://opa:8181")

# ── LiteLLM ──────────────────────────────────────────────────
LITELLM_CONFIG = os.getenv("LITELLM_CONFIG", "/app/litellm_config.yaml")

# ── Agent Loop ───────────────────────────────────────────────
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "10"))
TOOL_CALL_TIMEOUT = int(os.getenv("TOOL_CALL_TIMEOUT", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

# ── Tool Injection ───────────────────────────────────────────
TOOL_REFRESH_INTERVAL = int(os.getenv("TOOL_REFRESH_INTERVAL", "60"))
# Set to "false" when the client already has MCP access (e.g. OpenCode)
# and tool injection would waste input tokens.
TOOL_INJECTION_ENABLED = os.getenv("TOOL_INJECTION_ENABLED", "true").lower() == "true"

# ── Failure Mode ─────────────────────────────────────────────
# "closed" = return 503 if MCP server unreachable
# "open" = forward request without tool injection
FAIL_MODE = os.getenv("FAIL_MODE", "closed")

# ── Metrics ──────────────────────────────────────────────────
METRICS_PORT = int(os.getenv("METRICS_PORT", "9092"))

# ── PII Protection ───────────────────────────────────────────
INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")
PII_SCAN_ENABLED = os.getenv("PII_SCAN_ENABLED", "true").lower() == "true"
PII_SCAN_FORCED = os.getenv("PII_SCAN_FORCED", "false").lower() == "true"

# ── Connection Pool ──────────────────────────────────────────
PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "1"))
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "5"))

# ── Authentication ───────────────────────────────────────────
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").lower() == "true"
PG_HOST = os.getenv("PG_HOST", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DATABASE = os.getenv("PG_DATABASE", "powerbrain")
PG_USER = os.getenv("PG_USER", "pb_admin")
PG_PASSWORD = _read_secret("PG_PASSWORD", "changeme")

# ── MCP Servers ──────────────────────────────────────────────
MCP_SERVERS_CONFIG = os.getenv("MCP_SERVERS_CONFIG", "/app/mcp_servers.yaml")

# ── Provider Key Map (for passthrough routing) ───────────────
# Maps LiteLLM provider prefix → env var value.
# Only providers with a configured key are included.
# Used by passthrough routing to resolve API keys for models
# not listed in litellm_config.yaml.
PROVIDER_KEY_MAP: dict[str, str] = {}

_PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "github": "GITHUB_PAT",
    "azure": "AZURE_API_KEY",
    "cohere": "COHERE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

for _provider, _env_var in _PROVIDER_ENV_VARS.items():
    _key = _read_secret(_env_var, "")
    if _key:
        PROVIDER_KEY_MAP[_provider] = _key
        # Also export to os.environ for LiteLLM (if not already set)
        if _env_var not in os.environ:
            os.environ[_env_var] = _key
