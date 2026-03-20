# ============================================================
#  Wissensdatenbank – DSGVO / Datenschutz Policies
#  Paket: kb.privacy
#
#  Regelt: Zweckbindung, PII-Zugriff, Aufbewahrungsfristen,
#  Recht auf Löschung, Datenminimierung
# ============================================================

package kb.privacy

import rego.v1

# ── Zweckbindung (Art. 5 Abs. 1 lit. b DSGVO) ─────────────
# Jeder Zugriff auf PII-Daten muss einen gültigen Zweck angeben,
# der mit dem ursprünglichen Erhebungszweck kompatibel ist.

default purpose_allowed := false

# Erlaubte Verarbeitungszwecke pro Datenklasse
allowed_purposes := {
    "customer_data":   {"support", "billing", "contract_fulfillment"},
    "employee_data":   {"hr_management", "payroll"},
    "analytics_data":  {"reporting", "product_improvement"},
    "marketing_data":  {"campaign_management", "consent_based_contact"},
}

purpose_allowed if {
    not input.contains_pii
}

purpose_allowed if {
    input.contains_pii
    purposes := allowed_purposes[input.data_category]
    input.purpose in purposes
}

purpose_denied_reason := reason if {
    not purpose_allowed
    input.contains_pii
    reason := sprintf(
        "Zweckbindung verletzt: Zweck '%s' ist nicht erlaubt für Datenkategorie '%s'. Erlaubt: %v",
        [input.purpose, input.data_category, allowed_purposes[input.data_category]]
    )
}

# ── Datenminimierung (Art. 5 Abs. 1 lit. c DSGVO) ─────────
# Agenten erhalten nur die Felder, die für ihren Zweck nötig sind.

default fields_to_redact := set()

fields_to_redact := {"email", "phone", "address", "iban", "birthdate"} if {
    input.purpose == "reporting"
}

fields_to_redact := {"iban", "birthdate", "address"} if {
    input.purpose == "support"
}

fields_to_redact := {"email", "phone", "iban", "birthdate", "address"} if {
    input.purpose == "product_improvement"
}

# ── PII-Ingestion-Policy ───────────────────────────────────
# Entscheidet, wie PII bei der Ingestion behandelt wird.

default pii_action := "block"

# Public-Daten: PII immer maskieren
pii_action := "mask" if {
    input.classification == "public"
    input.contains_pii == true
}

# Internal: PII pseudonymisieren (reversibel mit Schlüssel)
pii_action := "pseudonymize" if {
    input.classification == "internal"
    input.contains_pii == true
}

# Confidential: PII speichern, aber verschlüsselt + zweckgebunden
pii_action := "encrypt_and_store" if {
    input.classification == "confidential"
    input.contains_pii == true
    input.legal_basis != ""
}

# Restricted: Nie PII in die Wissensdatenbank
pii_action := "block" if {
    input.classification == "restricted"
    input.contains_pii == true
}

# ── Aufbewahrungsfristen (Art. 5 Abs. 1 lit. e DSGVO) ──────

default retention_days := 365

retention_days := 90 if {
    input.data_category == "analytics_data"
}

retention_days := 730 if {
    input.data_category == "contract_data"
}

retention_days := 1095 if {
    input.data_category == "accounting_data"
}

# ── Recht auf Löschung (Art. 17 DSGVO) ─────────────────────

default deletion_allowed := false

deletion_allowed if {
    input.request_type == "erasure"
    input.data_subject_verified
    not has_legal_retention_obligation
}

has_legal_retention_obligation if {
    input.data_category == "accounting_data"
    input.age_days < 3650
}

has_legal_retention_obligation if {
    input.data_category == "contract_data"
    input.age_days < 1095
}

deletion_response := response if {
    deletion_allowed
    response := {
        "action": "delete",
        "scope": "all_related_records",
        "includes": ["postgresql_rows", "qdrant_vectors", "audit_anonymize"],
    }
}

deletion_response := response if {
    not deletion_allowed
    has_legal_retention_obligation
    response := {
        "action": "restrict",
        "reason": "Gesetzliche Aufbewahrungspflicht",
        "scope": "restrict_processing",
        "review_after_days": retention_days,
    }
}

# ── Dual Storage Policy ─────────────────────────────────────
# Bestimmt pro Klassifizierung, ob Original + Pseudonym gespeichert werden.
# Änderbar ohne Code-Deployment.

default dual_storage_enabled := false

dual_storage_enabled if {
    input.classification == "internal"
    input.contains_pii == true
}

dual_storage_enabled if {
    input.classification == "confidential"
    input.contains_pii == true
}

# ── Vault Access Policy ─────────────────────────────────────
# Prüft ob ein Agent auf Original-Daten im Vault zugreifen darf.
# Erfordert gültigen Token + Zweckbindung.

default vault_access_allowed := false

vault_access_allowed if {
    input.token_valid == true
    input.token_expired == false
    purpose_allowed_for_vault
    role_allowed_for_classification
}

purpose_allowed_for_vault if {
    some allowed_purpose in allowed_purposes[input.data_category]
    input.purpose == allowed_purpose
}

role_allowed_for_classification if {
    input.classification == "internal"
    input.agent_role in {"analyst", "admin", "developer"}
}

role_allowed_for_classification if {
    input.classification == "confidential"
    input.agent_role == "admin"
}

# ── Vault Field Redaction ───────────────────────────────────
# Welche Felder im Original redaktiert werden, abhängig vom Zweck.
# Nutzt gleiche Logik wie fields_to_redact, aber explizit für Vault.

default vault_fields_to_redact := {"email", "phone", "iban", "birthdate", "address"}

vault_fields_to_redact := {"email", "phone", "iban", "birthdate", "address"} if {
    input.purpose == "reporting"
}

vault_fields_to_redact := {"iban", "birthdate", "address"} if {
    input.purpose == "support"
}

vault_fields_to_redact := {"email", "phone", "iban", "birthdate", "address"} if {
    input.purpose == "product_improvement"
}

vault_fields_to_redact := {"birthdate"} if {
    input.purpose == "billing"
}

vault_fields_to_redact := set() if {
    input.purpose == "contract_fulfillment"
}
