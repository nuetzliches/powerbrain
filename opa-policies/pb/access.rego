# ============================================================
#  Wissensdatenbank – OPA Rego Policies
#  Paket: pb.access
# ============================================================

package pb.access

import rego.v1

# Default: Zugriff verweigert
default allow := false

# Public-Daten sind für alle zugänglich
allow if {
    input.classification == "public"
}

# Internal-Daten für Analysten und Admins
allow if {
    input.classification == "internal"
    input.agent_role in {"analyst", "admin", "developer"}
}

# Confidential-Daten nur für Admins
allow if {
    input.classification == "confidential"
    input.agent_role == "admin"
}

# Restricted: nur Admin + expliziter Zweck
allow if {
    input.classification == "restricted"
    input.agent_role == "admin"
    input.action == "read"
    # Zusätzliche Prüfungen können hier ergänzt werden
}

# Write-Zugriff nur für Admins und Developer
allow if {
    input.action == "write"
    input.agent_role in {"admin", "developer"}
    input.classification in {"public", "internal"}
}

# Deny-Reason für Debugging
reason := msg if {
    not allow
    msg := sprintf("Zugriff verweigert: Rolle '%s' darf nicht auf '%s'-Daten zugreifen (Aktion: %s)",
                   [input.agent_role, input.classification, input.action])
}

reason := "Zugriff erlaubt" if allow
