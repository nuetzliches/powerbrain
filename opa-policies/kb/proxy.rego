# ============================================================
#  Powerbrain – AI Provider Proxy Policies
#  Package: kb.proxy
#
#  Controls the AI Provider Proxy behavior:
#  - Which agent roles may use the proxy
#  - Which MCP tools are mandatory (injected into every request)
#  - Max agent-loop iterations per role
# ============================================================

package kb.proxy

import rego.v1

# ── Provider Access ──────────────────────────────────────────
# Which agent roles may use the proxy to access LLM providers.

default provider_allowed := false

provider_allowed if {
    input.agent_role in {"analyst", "developer", "admin"}
}

# ── Required Tools ───────────────────────────────────────────
# MCP tools that MUST be injected into every LLM request.
# The proxy merges these into the tools[] array transparently.

default required_tools := {"search_knowledge", "check_policy"}

# ── Max Iterations ───────────────────────────────────────────
# Maximum agent-loop iterations (tool-call cycles) per role.
# Prevents runaway loops.

default max_iterations := 5

max_iterations := 10 if {
    input.agent_role in {"developer", "admin"}
}

# ── PII Protection ───────────────────────────────────────────
# Controls whether outbound LLM requests are scanned for PII.
# Enabled by default; only admin may opt out (unless forced).

default pii_scan_enabled := true

pii_scan_forced if {
    input.pii_scan_forced == true
}

pii_scan_opt_out_allowed if {
    input.agent_role == "admin"
    input.pii_scan_opt_out == true
    not pii_scan_forced
}

pii_scan_enabled := false if {
    pii_scan_opt_out_allowed
}

pii_entity_types := {"PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "LOCATION"}

default pii_system_prompt_injection := true

# ── Non-Text Content ─────────────────────────────────────────
# Controls how non-text content (images, PDFs, etc.) is handled.
# Default: replace with placeholder; admin may allow passthrough.

default non_text_content_action := "placeholder"

non_text_content_action := "allow" if {
    input.agent_role == "admin"
}

# ── MCP Server Access ────────────────────────────────────────
# Controls which MCP servers each role may access.
# Default: only powerbrain. Developer and admin: all configured servers.

default mcp_servers_allowed := ["powerbrain"]

mcp_servers_allowed := input.configured_servers if {
    input.agent_role in {"developer", "admin"}
}
