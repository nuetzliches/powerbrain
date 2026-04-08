# ============================================================
#  Powerbrain – Human Oversight Policies
#  Package: pb.oversight
#
#  Data-driven: requires_approval_matrix from data.json
#
#  EU AI Act Art. 14 (human oversight). Decides whether a given
#  data-retrieval request needs human approval before it runs.
#
#  Input:
#    input.agent_role    — "viewer" | "analyst" | "developer" | "admin"
#    input.classification — "public" | "internal" | "confidential" | "restricted"
#    input.tool          — MCP tool name (e.g. "search_knowledge")
#
#  Output:
#    requires_approval — bool
#    approval_reason   — human-readable justification
# ============================================================

package pb.oversight

import rego.v1

# ── Does this request need a human in the loop? ─────────────
# Two-axis matrix keyed on classification → list of roles that
# need approval. Admin is always exempt (they are the approvers).

default requires_approval := false

requires_approval if {
    input.agent_role != "admin"
    roles_needing_approval := data.pb.config.human_oversight.requires_approval_matrix[input.classification]
    some r in roles_needing_approval
    r == input.agent_role
}

# ── Human-readable reason ────────────────────────────────────

default approval_reason := ""

approval_reason := msg if {
    requires_approval
    msg := sprintf(
        "Human approval required: classification=%q, role=%q (EU AI Act Art. 14)",
        [input.classification, input.agent_role],
    )
}

# ── Config accessors (convenience for clients) ──────────────

pending_review_timeout_minutes := t if {
    t := data.pb.config.human_oversight.pending_review_timeout_minutes
}

max_pending_per_agent := n if {
    n := data.pb.config.human_oversight.max_pending_per_agent
}
