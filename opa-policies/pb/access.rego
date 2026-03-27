# ============================================================
#  Wissensdatenbank – OPA Rego Policies
#  Paket: pb.access
#
#  Data-driven: role/classification matrix from data.json
# ============================================================

package pb.access

import rego.v1

# Default: Zugriff verweigert
default allow := false

# Read-Zugriff: Rolle muss in der access_matrix stehen
allow if {
    input.action != "write"
    some role in data.pb.config.access_matrix[input.classification]
    input.agent_role == role
}

# Write-Zugriff: nur konfigurierte Rollen auf konfigurierte Klassifizierungen
allow if {
    input.action == "write"
    some role in data.pb.config.write_roles
    input.agent_role == role
    some cls in data.pb.config.write_classifications
    input.classification == cls
}

# Deny-Reason für Debugging
reason := msg if {
    not allow
    msg := sprintf("Zugriff verweigert: Rolle '%s' darf nicht auf '%s'-Daten zugreifen (Aktion: %s)",
                   [input.agent_role, input.classification, input.action])
}

reason := "Zugriff erlaubt" if allow
