# ============================================================
#  Powerbrain – Ingestion Quality Policies
#  Package: pb.ingestion
#
#  Data-driven: min_quality_score per source_type from data.json
#
#  EU AI Act Art. 10 (data quality). Blocking gate: documents
#  whose computed quality_score is below the configured minimum
#  are rejected at ingestion time and logged to ingestion_rejections.
#
#  Input:
#    input.source_type    — e.g. "code", "contracts", "docs"
#    input.quality_score  — 0.0 .. 1.0 composite score
#
#  Output:
#    quality_gate.allowed    — bool
#    quality_gate.min_score  — the threshold that was applied
#    quality_gate.reason     — human-readable reason when denied
# ============================================================

package pb.ingestion

import rego.v1

# ── Resolve minimum score per source_type ────────────────────
# Falls back to "default" when no source-type-specific entry exists.

min_score_for_source(source_type) := score if {
    score := data.pb.config.ingestion.min_quality_score[source_type]
}

min_score_for_source(source_type) := score if {
    not data.pb.config.ingestion.min_quality_score[source_type]
    score := data.pb.config.ingestion.min_quality_score.default
}

# ── Quality gate decision ────────────────────────────────────

default quality_gate := {
    "allowed":   false,
    "min_score": 0.0,
    "reason":    "quality_score below minimum",
}

quality_gate := {
    "allowed":   true,
    "min_score": threshold,
    "reason":    "",
} if {
    threshold := min_score_for_source(input.source_type)
    input.quality_score >= threshold
}

quality_gate := {
    "allowed":   false,
    "min_score": threshold,
    "reason":    sprintf("quality_score %.3f < required %.3f for source_type %q",
                         [input.quality_score, threshold, input.source_type]),
} if {
    threshold := min_score_for_source(input.source_type)
    input.quality_score < threshold
}
