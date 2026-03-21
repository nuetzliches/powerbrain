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
