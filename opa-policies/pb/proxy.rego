# ============================================================
#  Powerbrain – AI Provider Proxy Policies
#  Package: pb.proxy
#
#  Data-driven: roles, thresholds, entity types from data.json
#
#  Controls the AI Provider Proxy behavior:
#  - Which agent roles may use the proxy
#  - Which MCP tools are mandatory (injected into every request)
#  - Max agent-loop iterations per role
# ============================================================

package pb.proxy

import rego.v1

# ── Provider Access ──────────────────────────────────────────
# Which agent roles may use the proxy to access LLM providers.

default provider_allowed := false

provider_allowed if {
    some role in data.pb.config.proxy.allowed_roles
    input.agent_role == role
}

# ── Required Tools ───────────────────────────────────────────
# MCP tools that MUST be injected into every LLM request.

default required_tools := set()

required_tools := {t | some t in data.pb.config.proxy.required_tools}

# ── Max Iterations ───────────────────────────────────────────
# Maximum agent-loop iterations (tool-call cycles) per role.

default max_iterations := 5

_is_elevated_role if {
    some role in data.pb.config.proxy.elevated_roles
    input.agent_role == role
}

max_iterations := data.pb.config.proxy.max_iterations.elevated if {
    _is_elevated_role
}

max_iterations := data.pb.config.proxy.max_iterations.default if {
    not _is_elevated_role
}

# ── PII Protection ───────────────────────────────────────────
# Controls whether outbound LLM requests are scanned for PII.

default pii_scan_enabled := true

default pii_scan_forced := true

pii_scan_forced := false if {
    input.agent_role == "admin"
    input.pii_scan_forced_override == false
}

pii_scan_opt_out_allowed if {
    input.agent_role == "admin"
    input.pii_scan_opt_out == true
    not pii_scan_forced
}

pii_scan_enabled := false if {
    pii_scan_opt_out_allowed
}

pii_entity_types := {t | some t in data.pb.config.pii_entity_types}

default pii_system_prompt_injection := true

# ── Non-Text Content ─────────────────────────────────────────

default non_text_content_action := "placeholder"

non_text_content_action := "allow" if {
    input.agent_role == "admin"
}

# ── MCP Server Access ────────────────────────────────────────

default mcp_servers_allowed := ["powerbrain"]

mcp_servers_allowed := input.configured_servers if {
    _is_elevated_role
}

mcp_servers_allowed := data.pb.config.proxy.default_mcp_servers if {
    not _is_elevated_role
}
