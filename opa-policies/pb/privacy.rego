# ============================================================
#  Wissensdatenbank – DSGVO / Datenschutz Policies
#  Paket: pb.privacy
#
#  Data-driven: purposes, retention, field redaction from data.json
#
#  Regelt: Zweckbindung, PII-Zugriff, Aufbewahrungsfristen,
#  Recht auf Löschung, Datenminimierung
# ============================================================

package pb.privacy

import rego.v1

# ── Zweckbindung (Art. 5 Abs. 1 lit. b DSGVO) ─────────────
# Jeder Zugriff auf PII-Daten muss einen gültigen Zweck angeben,
# der mit dem ursprünglichen Erhebungszweck kompatibel ist.

default purpose_allowed := false

# Erlaubte Verarbeitungszwecke aus data.json
allowed_purposes := data.pb.config.allowed_purposes

purpose_allowed if {
    not input.contains_pii
}

purpose_allowed if {
    input.contains_pii
    purposes := allowed_purposes[input.data_category]
    some p in purposes
    input.purpose == p
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

fields_to_redact := {f | some f in data.pb.config.fields_to_redact[input.purpose]} if {
    data.pb.config.fields_to_redact[input.purpose]
}

fields_to_redact := {f | some f in data.pb.config.fields_to_redact["default"]} if {
    not data.pb.config.fields_to_redact[input.purpose]
    input.purpose
}

# ── PII-Ingestion-Policy ───────────────────────────────────
# Entscheidet, wie PII bei der Ingestion behandelt wird.

default pii_action := "block"

pii_action := data.pb.config.pii_actions[input.classification] if {
    input.contains_pii == true
    data.pb.config.pii_actions[input.classification]
    # Confidential benötigt legal_basis
    input.classification != "confidential"
}

pii_action := "encrypt_and_store" if {
    input.contains_pii == true
    input.classification == "confidential"
    input.legal_basis != ""
}

pii_action := "block" if {
    input.contains_pii == true
    input.classification == "confidential"
    not input.legal_basis
}

# ── Aufbewahrungsfristen (Art. 5 Abs. 1 lit. e DSGVO) ──────

default retention_days := 365

retention_days := data.pb.config.retention_days[input.data_category] if {
    data.pb.config.retention_days[input.data_category]
}

# ── Recht auf Löschung (Art. 17 DSGVO) ─────────────────────

default deletion_allowed := false

deletion_allowed if {
    input.request_type == "erasure"
    input.data_subject_verified
    not has_legal_retention_obligation
}

has_legal_retention_obligation if {
    max_age := data.pb.config.retention_obligations[input.data_category]
    input.age_days < max_age
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

default vault_access_allowed := false

vault_access_allowed if {
    input.token_valid == true
    input.token_expired == false
    purpose_allowed_for_vault
    role_allowed_for_classification
}

purpose_allowed_for_vault if {
    purposes := allowed_purposes[input.data_category]
    some p in purposes
    input.purpose == p
}

role_allowed_for_classification if {
    some role in data.pb.config.access_matrix[input.classification]
    input.agent_role == role
    input.classification != "public"
}

# ── Vault Field Redaction ───────────────────────────────────
# Welche Felder im Original redaktiert werden, abhängig vom Zweck.

default vault_fields_to_redact := set()

vault_fields_to_redact := {f | some f in data.pb.config.fields_to_redact[input.purpose]} if {
    data.pb.config.fields_to_redact[input.purpose]
}

vault_fields_to_redact := {f | some f in data.pb.config.fields_to_redact["default"]} if {
    not data.pb.config.fields_to_redact[input.purpose]
}
