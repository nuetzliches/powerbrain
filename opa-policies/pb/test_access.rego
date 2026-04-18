package pb.access_test

import rego.v1
import data.pb.access

# ── Read Access by Classification ─────────────────────────────

test_public_allowed_for_viewer if {
    access.allow with input as {"agent_role": "viewer", "classification": "public", "action": "read"}
}

test_public_allowed_for_analyst if {
    access.allow with input as {"agent_role": "analyst", "classification": "public", "action": "read"}
}

test_public_allowed_for_admin if {
    access.allow with input as {"agent_role": "admin", "classification": "public", "action": "read"}
}

test_internal_allowed_for_analyst if {
    access.allow with input as {"agent_role": "analyst", "classification": "internal", "action": "read"}
}

test_internal_allowed_for_developer if {
    access.allow with input as {"agent_role": "developer", "classification": "internal", "action": "read"}
}

test_internal_allowed_for_admin if {
    access.allow with input as {"agent_role": "admin", "classification": "internal", "action": "read"}
}

test_internal_denied_for_viewer if {
    not access.allow with input as {"agent_role": "viewer", "classification": "internal", "action": "read"}
}

test_confidential_allowed_for_admin if {
    access.allow with input as {"agent_role": "admin", "classification": "confidential", "action": "read"}
}

# confidential covers routine business-internal records (customer profiles,
# salary bands, contracts); analysts and developers need read access for
# their day-to-day work. Only `restricted` (board- / audit-only) is locked
# down to admin.
test_confidential_allowed_for_analyst if {
    access.allow with input as {"agent_role": "analyst", "classification": "confidential", "action": "read"}
}

test_confidential_allowed_for_developer if {
    access.allow with input as {"agent_role": "developer", "classification": "confidential", "action": "read"}
}

# Demo sales-UI contract: viewer must NOT see confidential or restricted data.
test_confidential_denied_for_viewer if {
    not access.allow with input as {"agent_role": "viewer", "classification": "confidential", "action": "read"}
}

test_restricted_denied_for_viewer if {
    not access.allow with input as {"agent_role": "viewer", "classification": "restricted", "action": "read"}
}

test_restricted_allowed_for_admin if {
    access.allow with input as {"agent_role": "admin", "classification": "restricted", "action": "read"}
}

test_restricted_denied_for_analyst if {
    not access.allow with input as {"agent_role": "analyst", "classification": "restricted", "action": "read"}
}

# ── Write Access ──────────────────────────────────────────────

test_write_allowed_for_admin_public if {
    access.allow with input as {"agent_role": "admin", "classification": "public", "action": "write"}
}

test_write_allowed_for_developer_internal if {
    access.allow with input as {"agent_role": "developer", "classification": "internal", "action": "write"}
}

test_write_denied_for_analyst if {
    not access.allow with input as {"agent_role": "analyst", "classification": "public", "action": "write"}
}

test_write_denied_for_viewer if {
    not access.allow with input as {"agent_role": "viewer", "classification": "public", "action": "write"}
}

test_write_denied_on_confidential if {
    not access.allow with input as {"agent_role": "admin", "classification": "confidential", "action": "write"}
}

test_write_denied_on_restricted if {
    not access.allow with input as {"agent_role": "admin", "classification": "restricted", "action": "write"}
}

# ── Reason Messages ───────────────────────────────────────────

test_reason_denied if {
    access.reason == "Access denied: role 'viewer' is not allowed to access 'internal' data (action: read)" with input as {
        "agent_role": "viewer", "classification": "internal", "action": "read",
    }
}

test_reason_allowed if {
    access.reason == "Access allowed" with input as {
        "agent_role": "admin", "classification": "public", "action": "read",
    }
}
