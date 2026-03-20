# ============================================================
#  Wissensdatenbank – Business Rules
#  Paket: kb.rules
#  
#  Strategien und Geschäftsregeln, die Agenten bei
#  Entscheidungen berücksichtigen müssen.
# ============================================================

package kb.rules

import rego.v1

# ── Pricing-Regeln ──────────────────────────────────────────
pricing := rules if {
    rules := {
        "discount_max_percent": 15,
        "approval_required_above": 10000,
        "currency": "EUR",
        "rules": [
            "Rabatte über 10% erfordern Teamlead-Freigabe",
            "Neukunden erhalten maximal 5% Erstkundenrabatt",
            "Jahresverträge berechtigen zu 10% Rabatt",
        ]
    }
}

# ── Workflow-Regeln ─────────────────────────────────────────
workflow := rules if {
    rules := {
        "phases": ["Planung", "Umsetzung", "Review", "Abnahme"],
        "rules": [
            "Jede Phase muss dokumentiert werden",
            "Review erfordert mindestens 2 Reviewer",
            "Abnahme nur durch Projektleiter oder Kunde",
        ],
        "escalation": {
            "after_days": 5,
            "notify": ["projektleiter", "teamlead"]
        }
    }
}

# ── Compliance-Regeln ───────────────────────────────────────
compliance := rules if {
    rules := {
        "data_retention_days": 365,
        "gdpr_relevant": true,
        "rules": [
            "Personenbezogene Daten müssen nach 365 Tagen gelöscht werden",
            "Jeder Datenzugriff wird im Audit-Log protokolliert",
            "Export von confidential-Daten ist untersagt",
            "Datenverarbeitung nur innerhalb der EU",
        ]
    }
}

# ── Dynamische Regelauswertung ──────────────────────────────
# Agenten können Regeln nach Kategorie abfragen
rules_for_category := pricing if {
    input.context.category == "pricing"
}

rules_for_category := workflow if {
    input.context.category == "workflow"
}

rules_for_category := compliance if {
    input.context.category == "compliance"
}
