package pb.incidents_test

import rego.v1
import data.pb.incidents

# ── RBAC tests ─────────────────────────────────────────────

test_report_allowed_for_viewer if {
    incidents.allow_report with input as {"agent_role": "viewer"}
}

test_report_allowed_for_analyst if {
    incidents.allow_report with input as {"agent_role": "analyst"}
}

test_report_allowed_for_admin if {
    incidents.allow_report with input as {"agent_role": "admin"}
}

test_report_denied_for_unknown_role if {
    not incidents.allow_report with input as {"agent_role": "guest"}
}

test_list_admin_only if {
    incidents.allow_list with input as {"agent_role": "admin"}
    not incidents.allow_list with input as {"agent_role": "viewer"}
    not incidents.allow_list with input as {"agent_role": "analyst"}
    not incidents.allow_list with input as {"agent_role": "developer"}
}

test_assess_admin_only if {
    incidents.allow_assess with input as {"agent_role": "admin"}
    not incidents.allow_assess with input as {"agent_role": "analyst"}
}

test_notify_authority_admin_only if {
    incidents.allow_notify_authority with input as {"agent_role": "admin"}
    not incidents.allow_notify_authority with input as {"agent_role": "developer"}
}

test_notify_subject_admin_only if {
    incidents.allow_notify_subject with input as {"agent_role": "admin"}
    not incidents.allow_notify_subject with input as {"agent_role": "analyst"}
}

# ── Risk-score tests ───────────────────────────────────────

# No PII at all → score 0, not notifiable
test_empty_pii_zero_score if {
    incidents.risk_score == 0 with input as {
        "pii_types": [],
        "subjects": 1,
        "data_category": "internal",
    }
}

test_empty_pii_not_notifiable if {
    not incidents.notifiable with input as {
        "pii_types": [],
        "subjects": 1,
        "data_category": "internal",
    }
}

# Single low-PII hit, single subject, no category bonus → base * 1 * 1
test_single_low_pii_small_score if {
    incidents.risk_score == 5 with input as {
        "pii_types": ["ORG"],
        "subjects": 1,
        "data_category": "internal",
    }
}

# Single medium PII × 1 subject × default → 15
test_single_medium_pii if {
    incidents.risk_score == 15 with input as {
        "pii_types": ["PERSON"],
        "subjects": 1,
        "data_category": "internal",
    }
}

# Single high PII × 1 subject × default → 30 (below 50 threshold)
test_single_high_pii_below_threshold if {
    incidents.risk_score == 30 with input as {
        "pii_types": ["EMAIL_ADDRESS"],
        "subjects": 1,
        "data_category": "internal",
    }
    not incidents.notifiable with input as {
        "pii_types": ["EMAIL_ADDRESS"],
        "subjects": 1,
        "data_category": "internal",
    }
}

# Two high PII × 1 subject × default → 60, crosses threshold
test_two_high_pii_notifiable if {
    incidents.risk_score == 60 with input as {
        "pii_types": ["EMAIL_ADDRESS", "PHONE_NUMBER"],
        "subjects": 1,
        "data_category": "internal",
    }
    incidents.notifiable with input as {
        "pii_types": ["EMAIL_ADDRESS", "PHONE_NUMBER"],
        "subjects": 1,
        "data_category": "internal",
    }
}

# Subject multiplier kicks in at 10+
test_subject_bracket_10 if {
    # 1 high PII × 2.0 multiplier × default → 60
    incidents.risk_score == 60 with input as {
        "pii_types": ["EMAIL_ADDRESS"],
        "subjects": 10,
        "data_category": "internal",
    }
}

test_subject_bracket_100 if {
    # 1 high PII × 4.0 multiplier × default → 120
    incidents.risk_score == 120 with input as {
        "pii_types": ["EMAIL_ADDRESS"],
        "subjects": 100,
        "data_category": "internal",
    }
}

test_subject_bracket_1000 if {
    # 1 high PII × 8.0 multiplier × default → 240
    incidents.risk_score == 240 with input as {
        "pii_types": ["EMAIL_ADDRESS"],
        "subjects": 1000,
        "data_category": "internal",
    }
}

# Restricted-category multiplier doubles the score
test_restricted_doubles_score if {
    # 1 medium PII × 1 × 2.0 → 30
    incidents.risk_score == 30 with input as {
        "pii_types": ["PERSON"],
        "subjects": 1,
        "data_category": "restricted",
    }
}

# Confidential is 1.5×
test_confidential_15x if {
    # 1 medium PII × 1 × 1.5 → 22.5
    incidents.risk_score == 22.5 with input as {
        "pii_types": ["PERSON"],
        "subjects": 1,
        "data_category": "confidential",
    }
}

# Duplicates in pii_types do not double-count (set semantics)
test_dedup_pii_types if {
    incidents.risk_score == 30 with input as {
        "pii_types": ["EMAIL_ADDRESS", "EMAIL_ADDRESS"],
        "subjects": 1,
        "data_category": "internal",
    }
}

# Threshold edge: exactly at threshold counts as notifiable (≥)
test_threshold_exact_match if {
    # 5 low + threshold 50 — needs 10 lows. Or use 1 high (30) + 2 medium (30) = 60 already past.
    # Use case where score == threshold: 2 highs (60)? No, 60 ≠ 50.
    # Easiest: 50 threshold exactly via 1 medium (15) + 1 high (30) + 1 low (5) = 50.
    incidents.risk_score == 50 with input as {
        "pii_types": ["PERSON", "EMAIL_ADDRESS", "ORG"],
        "subjects": 1,
        "data_category": "internal",
    }
    incidents.notifiable with input as {
        "pii_types": ["PERSON", "EMAIL_ADDRESS", "ORG"],
        "subjects": 1,
        "data_category": "internal",
    }
}

# Breakdown is populated
test_breakdown_populated if {
    b := incidents.breakdown with input as {
        "pii_types": ["EMAIL_ADDRESS"],
        "subjects": 1,
        "data_category": "internal",
    }
    b.base_score == 30
    b.high_hits == 1
    b.medium_hits == 0
    b.low_hits == 0
    b.threshold == 50
    b.notifiable == false
}

# Deadline accessors
test_deadline_accessors if {
    incidents.notification_hours == 72
    incidents.warning_threshold_hours == 24
    incidents.critical_threshold_hours == 48
}
