package pb.summarization_test

import rego.v1
import data.pb.summarization

# ── summarize_allowed ────────────────────────────────────────

test_summarize_allowed_for_analyst if {
    summarization.summarize_allowed with input as {
        "agent_role": "analyst",
        "classification": "internal",
    }
}

test_summarize_allowed_for_admin if {
    summarization.summarize_allowed with input as {
        "agent_role": "admin",
        "classification": "internal",
    }
}

test_summarize_allowed_for_developer if {
    summarization.summarize_allowed with input as {
        "agent_role": "developer",
        "classification": "internal",
    }
}

test_summarize_denied_for_viewer if {
    not summarization.summarize_allowed with input as {
        "agent_role": "viewer",
        "classification": "internal",
    }
}

# ── summarize_required ───────────────────────────────────────

test_summarize_required_for_confidential if {
    summarization.summarize_required with input as {
        "agent_role": "analyst",
        "classification": "confidential",
    }
}

test_summarize_not_required_for_internal if {
    not summarization.summarize_required with input as {
        "agent_role": "analyst",
        "classification": "internal",
    }
}

test_summarize_not_required_for_public if {
    not summarization.summarize_required with input as {
        "agent_role": "analyst",
        "classification": "public",
    }
}

# ── summarize_detail ─────────────────────────────────────────

test_detail_brief_for_restricted if {
    summarization.summarize_detail == "brief" with input as {
        "agent_role": "admin",
        "classification": "restricted",
    }
}

test_detail_standard_for_internal if {
    summarization.summarize_detail == "standard" with input as {
        "agent_role": "analyst",
        "classification": "internal",
    }
}

test_detail_standard_for_confidential if {
    summarization.summarize_detail == "standard" with input as {
        "agent_role": "analyst",
        "classification": "confidential",
    }
}
