package kb.proxy_test

import rego.v1
import data.kb.proxy

# ── provider_allowed ─────────────────────────────────────────

test_provider_allowed_for_analyst if {
    proxy.provider_allowed with input as {
        "agent_role": "analyst",
        "provider": "gpt-4o",
    }
}

test_provider_allowed_for_admin if {
    proxy.provider_allowed with input as {
        "agent_role": "admin",
        "provider": "gpt-4o",
    }
}

test_provider_allowed_for_developer if {
    proxy.provider_allowed with input as {
        "agent_role": "developer",
        "provider": "gpt-4o",
    }
}

test_provider_denied_for_viewer if {
    not proxy.provider_allowed with input as {
        "agent_role": "viewer",
        "provider": "gpt-4o",
    }
}

# ── required_tools ───────────────────────────────────────────

test_required_tools_default if {
    proxy.required_tools == {"search_knowledge", "check_policy"} with input as {
        "agent_role": "analyst",
    }
}

# ── max_iterations ───────────────────────────────────────────

test_max_iterations_analyst if {
    proxy.max_iterations == 5 with input as {
        "agent_role": "analyst",
    }
}

test_max_iterations_developer if {
    proxy.max_iterations == 10 with input as {
        "agent_role": "developer",
    }
}

test_max_iterations_admin if {
    proxy.max_iterations == 10 with input as {
        "agent_role": "admin",
    }
}
