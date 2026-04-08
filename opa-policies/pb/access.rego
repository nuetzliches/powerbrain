# ============================================================
#  Knowledge base – OPA Rego policies
#  Package: pb.access
#
#  Data-driven: role/classification matrix from data.json
# ============================================================

package pb.access

import rego.v1

# Default: access denied
default allow := false

# Read access: role must be in the access_matrix
allow if {
    input.action != "write"
    some role in data.pb.config.access_matrix[input.classification]
    input.agent_role == role
}

# Write access: only configured roles on configured classifications
allow if {
    input.action == "write"
    some role in data.pb.config.write_roles
    input.agent_role == role
    some cls in data.pb.config.write_classifications
    input.classification == cls
}

# Deny reason for debugging
reason := msg if {
    not allow
    msg := sprintf("Access denied: role '%s' is not allowed to access '%s' data (action: %s)",
                   [input.agent_role, input.classification, input.action])
}

reason := "Access allowed" if allow
