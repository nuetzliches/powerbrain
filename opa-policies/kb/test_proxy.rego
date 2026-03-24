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

# ── PII Protection Tests ─────────────────────────────────────

test_pii_scan_enabled_default if {
    proxy.pii_scan_enabled with input as {"agent_role": "analyst"}
}

test_pii_scan_enabled_for_developer if {
    proxy.pii_scan_enabled with input as {"agent_role": "developer"}
}

test_pii_scan_admin_opt_out_allowed if {
    not proxy.pii_scan_enabled with input as {
        "agent_role": "admin",
        "pii_scan_opt_out": true,
    }
}

test_pii_scan_admin_opt_out_blocked_when_forced if {
    proxy.pii_scan_enabled with input as {
        "agent_role": "admin",
        "pii_scan_opt_out": true,
        "pii_scan_forced": true,
    }
}

test_pii_scan_non_admin_cannot_opt_out if {
    proxy.pii_scan_enabled with input as {
        "agent_role": "analyst",
        "pii_scan_opt_out": true,
    }
}

test_pii_entity_types_defined if {
    count(proxy.pii_entity_types) > 0
    "PERSON" in proxy.pii_entity_types
    "EMAIL_ADDRESS" in proxy.pii_entity_types
}

test_pii_scan_forced_default_false if {
    not proxy.pii_scan_forced with input as {}
}

# ── Non-Text Content Tests ───────────────────────────────────

test_non_text_default_placeholder if {
    proxy.non_text_content_action == "placeholder" with input as {"agent_role": "analyst"}
}

test_non_text_admin_allow if {
    proxy.non_text_content_action == "allow" with input as {"agent_role": "admin"}
}

test_non_text_developer_placeholder if {
    proxy.non_text_content_action == "placeholder" with input as {"agent_role": "developer"}
}

# ── MCP Servers Allowed ──────────────────────────────────────

test_mcp_servers_default_powerbrain if {
    proxy.mcp_servers_allowed == ["powerbrain"] with input as {
        "agent_role": "analyst",
        "configured_servers": ["powerbrain", "github"],
    }
}

test_mcp_servers_developer_all if {
    proxy.mcp_servers_allowed == ["powerbrain", "github"] with input as {
        "agent_role": "developer",
        "configured_servers": ["powerbrain", "github"],
    }
}

test_mcp_servers_admin_all if {
    proxy.mcp_servers_allowed == ["powerbrain", "github", "tools"] with input as {
        "agent_role": "admin",
        "configured_servers": ["powerbrain", "github", "tools"],
    }
}
