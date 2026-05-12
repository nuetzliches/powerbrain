# ============================================================
#  Powerbrain – Privacy Incident Workflow Policies
#  Package: pb.incidents
#
#  GDPR Art. 33/34: governs who may report, assess and notify
#  for privacy incidents, plus the risk-score calculator that
#  the `assess_incident` MCP tool uses to populate
#  notifiable_risk.
#
#  Data-driven: roles, weights, thresholds in data.json.
# ============================================================

package pb.incidents

import rego.v1

# ── RBAC ────────────────────────────────────────────────────

default allow_report := false
allow_report if {
    some r in data.pb.config.incidents.roles.allow_report
    input.agent_role == r
}

default allow_list := false
allow_list if {
    some r in data.pb.config.incidents.roles.allow_list
    input.agent_role == r
}

default allow_assess := false
allow_assess if {
    some r in data.pb.config.incidents.roles.allow_assess
    input.agent_role == r
}

default allow_notify_authority := false
allow_notify_authority if {
    some r in data.pb.config.incidents.roles.allow_notify_authority
    input.agent_role == r
}

default allow_notify_subject := false
allow_notify_subject if {
    some r in data.pb.config.incidents.roles.allow_notify_subject
    input.agent_role == r
}

# ── Risk Scoring (input: pii_types, subjects, data_category) ─

# PII-type weight sets (from data.json)
high_set := {t | some t in data.pb.config.incidents.risk_score.high_pii_types}
medium_set := {t | some t in data.pb.config.incidents.risk_score.medium_pii_types}
low_set := {t | some t in data.pb.config.incidents.risk_score.low_pii_types}

# Per-class unique hit counts. Empty / missing input.pii_types
# yields an empty set → count() = 0, so the calculator behaves
# gracefully when called for a not-yet-classified incident.
high_hits := count({t | some t in input.pii_types; high_set[t]})
medium_hits := count({t | some t in input.pii_types; medium_set[t]})
low_hits := count({t | some t in input.pii_types; low_set[t]})

weights := data.pb.config.incidents.risk_score.weights

base_score := s if {
    s := (high_hits * weights.high) +
         (medium_hits * weights.medium) +
         (low_hits * weights.low)
}

# Subject-count multiplier: pick the max bracket whose `min` is
# ≤ input.subjects. Default to 1.0 when no bracket matches
# (e.g. subjects = 0 or missing).
applicable_subject_multipliers := {m |
    some bracket in data.pb.config.incidents.risk_score.subject_multipliers
    input.subjects >= bracket.min
    m := bracket.multiplier
}

subject_multiplier := m if {
    count(applicable_subject_multipliers) > 0
    m := max(applicable_subject_multipliers)
}

subject_multiplier := 1.0 if {
    count(applicable_subject_multipliers) == 0
}

# Category multiplier: restricted > confidential > default.
is_elevated_category if {
    input.data_category == "restricted"
}

is_elevated_category if {
    input.data_category == "confidential"
}

category_multiplier := data.pb.config.incidents.risk_score.category_multiplier_restricted if {
    input.data_category == "restricted"
}

category_multiplier := data.pb.config.incidents.risk_score.category_multiplier_confidential if {
    input.data_category == "confidential"
}

category_multiplier := 1.0 if {
    not is_elevated_category
}

# Final risk score
default risk_score := 0

risk_score := s if {
    s := base_score * subject_multiplier * category_multiplier
}

# Notifiable decision
notifiable_threshold := data.pb.config.incidents.risk_score.notifiable_threshold

default notifiable := false
notifiable if {
    risk_score >= notifiable_threshold
}

# Breakdown for transparency / audit
breakdown := b if {
    b := {
        "base_score": base_score,
        "high_hits": high_hits,
        "medium_hits": medium_hits,
        "low_hits": low_hits,
        "subject_multiplier": subject_multiplier,
        "category_multiplier": category_multiplier,
        "risk_score": risk_score,
        "threshold": notifiable_threshold,
        "notifiable": notifiable,
    }
}

# ── Deadline config accessors ──────────────────────────────

notification_hours := data.pb.config.incidents.deadline.notification_hours
warning_threshold_hours := data.pb.config.incidents.deadline.warning_threshold_hours
critical_threshold_hours := data.pb.config.incidents.deadline.critical_threshold_hours
