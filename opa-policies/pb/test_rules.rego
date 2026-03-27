package pb.rules_test

import rego.v1
import data.pb.rules

# ── Category Lookup ───────────────────────────────────────────

test_pricing_category if {
    result := rules.rules_for_category with input as {"context": {"category": "pricing"}}
    result.discount_max_percent == 15
    result.currency == "EUR"
}

test_workflow_category if {
    result := rules.rules_for_category with input as {"context": {"category": "workflow"}}
    result.phases[0] == "Planung"
    result.escalation.after_days == 5
}

test_compliance_category if {
    result := rules.rules_for_category with input as {"context": {"category": "compliance"}}
    result.gdpr_relevant == true
    result.data_retention_days == 365
}

# ── Direct Accessors ─────────────────────────────────────────

test_pricing_direct if {
    rules.pricing.approval_required_above == 10000
}

test_workflow_direct if {
    count(rules.workflow.phases) == 4
}

test_compliance_direct if {
    count(rules.compliance.rules) == 4
}
