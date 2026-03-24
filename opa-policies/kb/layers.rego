# ============================================================
#  Powerbrain – Layer Access Control Policies
#  Package: kb.layers
#
#  Controls which context layers (L0/L1/L2) each agent role
#  can access based on data classification:
#  - L0: abstract (metadata only)
#  - L1: overview (summarized content)
#  - L2: full (raw content)
#
#  Admin can always access L2. For confidential data,
#  non-admins are limited to L1. For restricted data,
#  non-admins are limited to L0. Viewers can only access
#  L0 for internal data.
# ============================================================

package kb.layers

import rego.v1

# ── Max Layer ────────────────────────────────────────────────
# Determines the highest layer a role may access for a
# given classification level.

default max_layer := "L2"

max_layer := "L2" if { input.agent_role == "admin" }

max_layer := "L1" if {
    input.classification == "confidential"
    input.agent_role != "admin"
}

max_layer := "L0" if {
    input.classification == "restricted"
    input.agent_role != "admin"
}

max_layer := "L0" if {
    input.classification == "internal"
    input.agent_role == "viewer"
}

# ── Layer Order ──────────────────────────────────────────────

layer_order := {"L0": 0, "L1": 1, "L2": 2}

# ── Layer Allowed ────────────────────────────────────────────
# True when the requested layer does not exceed the max layer.

default layer_allowed := false

layer_allowed if {
    layer_order[input.requested_layer] <= layer_order[max_layer]
}
