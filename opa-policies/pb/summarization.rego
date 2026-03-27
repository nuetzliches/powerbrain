# ============================================================
#  Powerbrain – Context Summarization Policies
#  Package: pb.summarization
#
#  Data-driven: roles, classifications from data.json
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
# All roles except denied roles may request summaries.

default summarize_allowed := false

summarize_allowed if {
    not input.agent_role in {r | some r in data.pb.config.summarization.denied_roles}
}

# ── Summarize Required ───────────────────────────────────────
# Certain classifications require summarization (never raw chunks).

default summarize_required := false

summarize_required if {
    some cls in data.pb.config.summarization.required_classifications
    input.classification == cls
}

# ── Detail Level ─────────────────────────────────────────────
# Controls summary granularity. Some classifications get brief only.

default summarize_detail := "standard"

summarize_detail := "brief" if {
    some cls in data.pb.config.summarization.brief_classifications
    input.classification == cls
}
