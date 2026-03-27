# ============================================================
#  Wissensdatenbank – Business Rules
#  Paket: pb.rules
#
#  Data-driven: all rule definitions from data.json
#
#  Strategien und Geschäftsregeln, die Agenten bei
#  Entscheidungen berücksichtigen müssen.
# ============================================================

package pb.rules

import rego.v1

# ── Regelkategorien aus data.json ───────────────────────────
pricing := data.pb.config.rules.pricing

workflow := data.pb.config.rules.workflow

compliance := data.pb.config.rules.compliance

# ── Dynamische Regelauswertung ──────────────────────────────
# Agenten können Regeln nach Kategorie abfragen
rules_for_category := data.pb.config.rules[input.context.category] if {
    data.pb.config.rules[input.context.category]
}
