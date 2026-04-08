package pb.oversight_test

import rego.v1
import data.pb.oversight

# ── Admin is always exempt ─────────────────────────────────

test_admin_never_needs_approval_for_restricted if {
    not oversight.requires_approval with input as {
        "agent_role": "admin",
        "classification": "restricted",
        "tool": "search_knowledge",
    }
}

test_admin_never_needs_approval_for_confidential if {
    not oversight.requires_approval with input as {
        "agent_role": "admin",
        "classification": "confidential",
        "tool": "query_data",
    }
}

# ── Public data: no approval required ──────────────────────

test_public_no_approval_for_analyst if {
    not oversight.requires_approval with input as {
        "agent_role": "analyst",
        "classification": "public",
        "tool": "search_knowledge",
    }
}

# ── Internal data: no approval required ────────────────────

test_internal_no_approval_for_analyst if {
    not oversight.requires_approval with input as {
        "agent_role": "analyst",
        "classification": "internal",
        "tool": "search_knowledge",
    }
}

# ── Confidential: analyst needs approval ───────────────────
# (data.json configures: confidential → [analyst, developer])

test_confidential_requires_approval_for_analyst if {
    oversight.requires_approval with input as {
        "agent_role": "analyst",
        "classification": "confidential",
        "tool": "search_knowledge",
    }
}

test_confidential_requires_approval_for_developer if {
    oversight.requires_approval with input as {
        "agent_role": "developer",
        "classification": "confidential",
        "tool": "get_code_context",
    }
}

# ── Restricted: everyone except admin needs approval ───────

test_restricted_requires_approval_for_developer if {
    oversight.requires_approval with input as {
        "agent_role": "developer",
        "classification": "restricted",
        "tool": "search_knowledge",
    }
}

test_restricted_requires_approval_for_analyst if {
    oversight.requires_approval with input as {
        "agent_role": "analyst",
        "classification": "restricted",
        "tool": "query_data",
    }
}

# ── Reason is non-empty when approval required ─────────────

test_reason_populated_when_required if {
    oversight.approval_reason != "" with input as {
        "agent_role": "analyst",
        "classification": "confidential",
        "tool": "search_knowledge",
    }
}

test_reason_empty_when_not_required if {
    oversight.approval_reason == "" with input as {
        "agent_role": "analyst",
        "classification": "public",
        "tool": "search_knowledge",
    }
}

# ── Config accessors ────────────────────────────────────────

test_pending_review_timeout_exposed if {
    oversight.pending_review_timeout_minutes > 0
}

test_max_pending_per_agent_exposed if {
    oversight.max_pending_per_agent > 0
}
