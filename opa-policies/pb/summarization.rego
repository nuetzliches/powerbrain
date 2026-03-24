# ============================================================
#  Powerbrain – Context Summarization Policies
#  Package: pb.summarization
#
#  Controls whether search results are summarized before
#  delivery to agents. Supports three modes:
#  - allowed: agent may request summaries
#  - required: only summaries, never raw chunks (privacy)
#  - detail level: brief / standard / detailed
# ============================================================

package pb.summarization

import rego.v1

# ── Summarize Allowed ────────────────────────────────────────
# All roles except viewer may request summaries.

default summarize_allowed := false

summarize_allowed if {
    input.agent_role != "viewer"
}

# ── Summarize Required ───────────────────────────────────────
# Confidential data: only summaries, never raw chunks.
# This is a privacy enhancement — the agent gets the information
# but never the original text.

default summarize_required := false

summarize_required if {
    input.classification == "confidential"
}

# ── Detail Level ─────────────────────────────────────────────
# Controls summary granularity. Restricted data gets brief only.

default summarize_detail := "standard"

summarize_detail := "brief" if {
    input.classification == "restricted"
}
