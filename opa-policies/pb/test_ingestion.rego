package pb.ingestion_test

import rego.v1
import data.pb.ingestion

# ── Default threshold applied when source_type is unknown ───

test_default_threshold_passes if {
    result := ingestion.quality_gate with input as {
        "source_type":   "unknown",
        "quality_score": 0.9,
    }
    result.allowed == true
    result.min_score == 0.6
}

test_default_threshold_blocks if {
    result := ingestion.quality_gate with input as {
        "source_type":   "unknown",
        "quality_score": 0.5,
    }
    result.allowed == false
    result.min_score == 0.6
}

# ── Source-specific thresholds override default ─────────────

test_code_threshold_lower_passes if {
    result := ingestion.quality_gate with input as {
        "source_type":   "code",
        "quality_score": 0.45,
    }
    result.allowed == true
    result.min_score == 0.4
}

test_code_threshold_blocks_below_own_minimum if {
    result := ingestion.quality_gate with input as {
        "source_type":   "code",
        "quality_score": 0.35,
    }
    result.allowed == false
    result.min_score == 0.4
}

test_contracts_threshold_stricter if {
    result := ingestion.quality_gate with input as {
        "source_type":   "contracts",
        "quality_score": 0.7,
    }
    result.allowed == false
    result.min_score == 0.8
}

test_contracts_threshold_passes_at_high_score if {
    result := ingestion.quality_gate with input as {
        "source_type":   "contracts",
        "quality_score": 0.85,
    }
    result.allowed == true
    result.min_score == 0.8
}

# ── Exactly-at-threshold is allowed (>=) ───────────────────

test_exactly_at_threshold_is_allowed if {
    result := ingestion.quality_gate with input as {
        "source_type":   "unknown",
        "quality_score": 0.6,
    }
    result.allowed == true
}

# ── Reason field is set on deny ────────────────────────────

test_reason_populated_on_deny if {
    result := ingestion.quality_gate with input as {
        "source_type":   "contracts",
        "quality_score": 0.2,
    }
    result.allowed == false
    result.reason != ""
}

test_reason_empty_on_allow if {
    result := ingestion.quality_gate with input as {
        "source_type":   "code",
        "quality_score": 0.9,
    }
    result.allowed == true
    result.reason == ""
}

# ── GitHub source type (adapter) ──────────────────────────

test_github_threshold_low_passes if {
    result := ingestion.quality_gate with input as {
        "source_type":   "github",
        "quality_score": 0.35,
    }
    result.allowed == true
    result.min_score == 0.3
}

test_github_threshold_blocks_below_minimum if {
    result := ingestion.quality_gate with input as {
        "source_type":   "github",
        "quality_score": 0.25,
    }
    result.allowed == false
    result.min_score == 0.3
}
