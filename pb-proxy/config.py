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
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")

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

# ── LLM Provider Keys ───────────────────────────────────────
# Read from Docker Secret and export to os.environ so LiteLLM
# can pick it up via "os.environ/GITHUB_PAT" in litellm_config.yaml.
GITHUB_PAT = _read_secret("GITHUB_PAT", "")
if GITHUB_PAT:
    os.environ["GITHUB_PAT"] = GITHUB_PAT
