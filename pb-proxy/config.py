"""
pb-proxy configuration.
Reads from environment variables with sensible defaults.
Supports Docker Secrets via _FILE suffix convention.
"""

import os
import logging
import yaml

from shared.config import read_secret as _read_secret
from shared.ingestion_auth import verify_ingestion_auth_configured

log = logging.getLogger("pb-proxy")


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
# Per-tool-call deadline for MCP round-trips from the agent loop.
# Must sit **above** the MCP server's SUMMARIZATION_TIMEOUT (default
# 15 s) so the graceful "summary failed → raw chunks" fallback in
# mcp-server/server.py:summarize_text() reliably lands before the proxy
# abandons the connection. With a local Ollama LLM on CPU, 60 s gives
# enough headroom for the summary attempt + fallback + response. Lower
# it only when pointing at a hosted provider with sub-second latency.
TOOL_CALL_TIMEOUT = int(os.getenv("TOOL_CALL_TIMEOUT", "60"))
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
# B-50: shared service token to talk to the ingestion service.
INGESTION_AUTH_TOKEN = _read_secret("INGESTION_AUTH_TOKEN", "")
PII_SCAN_ENABLED = os.getenv("PII_SCAN_ENABLED", "true").lower() == "true"
PII_SCAN_FORCED = os.getenv("PII_SCAN_FORCED", "true").lower() == "true"


def ingestion_headers() -> dict[str, str]:
    """Auth headers for internal ingestion calls; empty when unset."""
    return (
        {"Authorization": f"Bearer {INGESTION_AUTH_TOKEN}"}
        if INGESTION_AUTH_TOKEN
        else {}
    )

# ── Connection Pool ──────────────────────────────────────────
PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "1"))
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "5"))

# ── Authentication ───────────────────────────────────────────
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").lower() == "true"
SKIP_INGESTION_AUTH_STARTUP_CHECK = (
    os.getenv("SKIP_INGESTION_AUTH_STARTUP_CHECK", "false").lower() == "true"
)
# Fail-closed: refuse to start with empty token + AUTH_REQUIRED=true (#126).
verify_ingestion_auth_configured(
    INGESTION_AUTH_TOKEN,
    auth_required=AUTH_REQUIRED,
    skip_check=SKIP_INGESTION_AUTH_STARTUP_CHECK,
    service_name="pb-proxy",
)
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


def load_provider_key_config(config_path: str | None = None) -> dict[str, str]:
    """Load provider_keys section from litellm_config.yaml.
    
    Returns dict mapping provider name → key_source ('central'|'user'|'hybrid').
    Defaults to 'central' for unconfigured providers.
    """
    path = config_path or LITELLM_CONFIG
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    
    raw = cfg.get("provider_keys", {}) or {}
    result = {}
    for provider, source in raw.items():
        if source not in ("central", "user", "hybrid"):
            log.warning("Invalid key_source '%s' for provider '%s', using 'central'", source, provider)
            source = "central"
        result[provider] = source
    return result
