# ============================================================
#  Knowledge base – GDPR / privacy policies
#  Package: pb.privacy
#
#  Data-driven: purposes, retention, field redaction from data.json
#
#  Governs: purpose binding, PII access, retention periods,
#  right to erasure, data minimization
# ============================================================

package pb.privacy

import rego.v1

# ── Purpose binding (Art. 5(1)(b) GDPR) ───────────────────
# Every access to PII data must specify a valid purpose that
# is compatible with the original collection purpose.

default purpose_allowed := false

# Allowed processing purposes from data.json
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
        "Purpose binding violated: purpose '%s' is not allowed for data category '%s'. Allowed: %v",
        [input.purpose, input.data_category, allowed_purposes[input.data_category]]
    )
}

# ── Data minimization (Art. 5(1)(c) GDPR) ─────────────────
# Agents only receive the fields required for their purpose.

default fields_to_redact := set()

fields_to_redact := {f | some f in data.pb.config.fields_to_redact[input.purpose]} if {
    data.pb.config.fields_to_redact[input.purpose]
}

fields_to_redact := {f | some f in data.pb.config.fields_to_redact["default"]} if {
    not data.pb.config.fields_to_redact[input.purpose]
    input.purpose
}

# ── PII ingestion policy ───────────────────────────────────
# Decides how PII is handled during ingestion.

default pii_action := "block"

pii_action := data.pb.config.pii_actions[input.classification] if {
    input.contains_pii == true
    data.pb.config.pii_actions[input.classification]
    # Confidential requires legal_basis
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

# ── Retention periods (Art. 5(1)(e) GDPR) ──────────────────

default retention_days := 365

retention_days := data.pb.config.retention_days[input.data_category] if {
    data.pb.config.retention_days[input.data_category]
}

# ── Right to erasure (Art. 17 GDPR) ─────────────────────────

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
        "reason": "Legal retention obligation",
        "scope": "restrict_processing",
        "review_after_days": retention_days,
    }
}

# ── Dual Storage Policy ─────────────────────────────────────
# Determines per classification whether original + pseudonym are stored.

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
# Checks whether an agent may access original data in the vault.

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
# Which fields in the original are redacted, depending on the purpose.

default vault_fields_to_redact := set()

vault_fields_to_redact := {f | some f in data.pb.config.fields_to_redact[input.purpose]} if {
    data.pb.config.fields_to_redact[input.purpose]
}

vault_fields_to_redact := {f | some f in data.pb.config.fields_to_redact["default"]} if {
    not data.pb.config.fields_to_redact[input.purpose]
}
