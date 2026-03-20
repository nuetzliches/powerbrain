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
    input.pii_detected
}

# Internal: PII pseudonymisieren (reversibel mit Schlüssel)
pii_action := "pseudonymize" if {
    input.classification == "internal"
    input.pii_detected
}

# Confidential: PII speichern, aber verschlüsselt + zweckgebunden
pii_action := "encrypt_and_store" if {
    input.classification == "confidential"
    input.pii_detected
    input.legal_basis != ""
}

# Restricted: Nie PII in die Wissensdatenbank
pii_action := "block" if {
    input.classification == "restricted"
    input.pii_detected
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
