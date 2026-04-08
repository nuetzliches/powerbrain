# ============================================================
#  Knowledge base – business rules
#  Package: pb.rules
#
#  Data-driven: all rule definitions from data.json
#
#  Strategies and business rules that agents must
#  consider when making decisions.
# ============================================================

package pb.rules

import rego.v1

# ── Rule categories from data.json ──────────────────────────
pricing := data.pb.config.rules.pricing

workflow := data.pb.config.rules.workflow

compliance := data.pb.config.rules.compliance

# ── Dynamic rule evaluation ─────────────────────────────────
# Agents can query rules by category
rules_for_category := data.pb.config.rules[input.context.category] if {
    data.pb.config.rules[input.context.category]
}
