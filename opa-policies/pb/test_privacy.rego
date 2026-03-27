package pb.privacy_test

import rego.v1
import data.pb.privacy

# ── Purpose Binding ───────────────────────────────────────────

test_purpose_allowed_no_pii if {
    privacy.purpose_allowed with input as {
        "contains_pii": false,
        "data_category": "customer_data",
        "purpose": "anything",
    }
}

test_purpose_allowed_valid if {
    privacy.purpose_allowed with input as {
        "contains_pii": true,
        "data_category": "customer_data",
        "purpose": "support",
    }
}

test_purpose_denied_invalid if {
    not privacy.purpose_allowed with input as {
        "contains_pii": true,
        "data_category": "customer_data",
        "purpose": "marketing",
    }
}

test_purpose_allowed_employee_hr if {
    privacy.purpose_allowed with input as {
        "contains_pii": true,
        "data_category": "employee_data",
        "purpose": "hr_management",
    }
}

test_purpose_denied_employee_wrong if {
    not privacy.purpose_allowed with input as {
        "contains_pii": true,
        "data_category": "employee_data",
        "purpose": "reporting",
    }
}

# ── Fields to Redact ──────────────────────────────────────────

test_redact_reporting if {
    fields := privacy.fields_to_redact with input as {"purpose": "reporting"}
    "email" in fields
    "phone" in fields
    "iban" in fields
}

test_redact_support if {
    fields := privacy.fields_to_redact with input as {"purpose": "support"}
    "iban" in fields
    "birthdate" in fields
    not "email" in fields
}

test_redact_billing if {
    fields := privacy.fields_to_redact with input as {"purpose": "billing"}
    "birthdate" in fields
    not "email" in fields
    not "phone" in fields
}

test_redact_contract_fulfillment_empty if {
    fields := privacy.fields_to_redact with input as {"purpose": "contract_fulfillment"}
    count(fields) == 0
}

# ── PII Action ────────────────────────────────────────────────

test_pii_action_public_mask if {
    privacy.pii_action == "mask" with input as {
        "contains_pii": true,
        "classification": "public",
    }
}

test_pii_action_internal_pseudonymize if {
    privacy.pii_action == "pseudonymize" with input as {
        "contains_pii": true,
        "classification": "internal",
    }
}

test_pii_action_confidential_with_basis if {
    privacy.pii_action == "encrypt_and_store" with input as {
        "contains_pii": true,
        "classification": "confidential",
        "legal_basis": "Art. 6 Abs. 1 lit. b DSGVO",
    }
}

test_pii_action_confidential_no_basis_block if {
    privacy.pii_action == "block" with input as {
        "contains_pii": true,
        "classification": "confidential",
    }
}

test_pii_action_restricted_block if {
    privacy.pii_action == "block" with input as {
        "contains_pii": true,
        "classification": "restricted",
    }
}

# ── Retention Days ────────────────────────────────────────────

test_retention_default if {
    privacy.retention_days == 365 with input as {
        "data_category": "customer_data",
    }
}

test_retention_analytics if {
    privacy.retention_days == 90 with input as {
        "data_category": "analytics_data",
    }
}

test_retention_contract if {
    privacy.retention_days == 730 with input as {
        "data_category": "contract_data",
    }
}

test_retention_accounting if {
    privacy.retention_days == 1095 with input as {
        "data_category": "accounting_data",
    }
}

# ── Deletion (Art. 17) ───────────────────────────────────────

test_deletion_allowed if {
    privacy.deletion_allowed with input as {
        "request_type": "erasure",
        "data_subject_verified": true,
        "data_category": "customer_data",
        "age_days": 500,
    }
}

test_deletion_denied_accounting_obligation if {
    not privacy.deletion_allowed with input as {
        "request_type": "erasure",
        "data_subject_verified": true,
        "data_category": "accounting_data",
        "age_days": 500,
    }
}

test_deletion_denied_contract_obligation if {
    not privacy.deletion_allowed with input as {
        "request_type": "erasure",
        "data_subject_verified": true,
        "data_category": "contract_data",
        "age_days": 500,
    }
}

# ── Dual Storage ──────────────────────────────────────────────

test_dual_storage_internal_pii if {
    privacy.dual_storage_enabled with input as {
        "classification": "internal",
        "contains_pii": true,
    }
}

test_dual_storage_public_no if {
    not privacy.dual_storage_enabled with input as {
        "classification": "public",
        "contains_pii": true,
    }
}

# ── Vault Access ──────────────────────────────────────────────

test_vault_access_allowed if {
    privacy.vault_access_allowed with input as {
        "token_valid": true,
        "token_expired": false,
        "contains_pii": true,
        "data_category": "customer_data",
        "purpose": "support",
        "classification": "internal",
        "agent_role": "analyst",
    }
}

test_vault_access_denied_expired_token if {
    not privacy.vault_access_allowed with input as {
        "token_valid": true,
        "token_expired": true,
        "contains_pii": true,
        "data_category": "customer_data",
        "purpose": "support",
        "classification": "internal",
        "agent_role": "analyst",
    }
}

test_vault_access_denied_wrong_purpose if {
    not privacy.vault_access_allowed with input as {
        "token_valid": true,
        "token_expired": false,
        "contains_pii": true,
        "data_category": "customer_data",
        "purpose": "marketing",
        "classification": "internal",
        "agent_role": "analyst",
    }
}
