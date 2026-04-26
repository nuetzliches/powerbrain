package pb.proxy_test

import rego.v1
import data.pb.proxy

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

test_pii_scan_admin_opt_out_blocked_by_default if {
    proxy.pii_scan_enabled with input as {
        "agent_role": "admin",
        "pii_scan_opt_out": true,
    }
}

test_pii_scan_admin_opt_out_blocked_when_forced if {
    proxy.pii_scan_enabled with input as {
        "agent_role": "admin",
        "pii_scan_opt_out": true,
    }
}

test_pii_scan_admin_opt_out_allowed_when_override if {
    not proxy.pii_scan_enabled with input as {
        "agent_role": "admin",
        "pii_scan_opt_out": true,
        "pii_scan_forced_override": false,
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

test_pii_scan_forced_default_true if {
    proxy.pii_scan_forced with input as {}
}

test_pii_scan_forced_admin_override if {
    not proxy.pii_scan_forced with input as {
        "agent_role": "admin",
        "pii_scan_forced_override": false,
    }
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

# ── Document Attachments ─────────────────────────────────────

test_documents_allowed_for_analyst if {
    proxy.documents_allowed with input as {"agent_role": "analyst"}
}

test_documents_allowed_for_developer if {
    proxy.documents_allowed with input as {"agent_role": "developer"}
}

test_documents_allowed_for_admin if {
    proxy.documents_allowed with input as {"agent_role": "admin"}
}

test_documents_denied_for_viewer if {
    not proxy.documents_allowed with input as {"agent_role": "viewer"}
}

test_documents_max_bytes_for_analyst if {
    proxy.documents_max_bytes == 25000000 with input as {"agent_role": "analyst"}
}

test_documents_max_bytes_zero_for_viewer if {
    proxy.documents_max_bytes == 0 with input as {"agent_role": "viewer"}
}

test_documents_allowed_mime_types_contains_pdf if {
    "application/pdf" in proxy.documents_allowed_mime_types with input as {
        "agent_role": "analyst",
    }
}

test_documents_allowed_mime_types_empty_for_viewer if {
    proxy.documents_allowed_mime_types == set() with input as {"agent_role": "viewer"}
}

test_documents_max_files_default_for_analyst if {
    proxy.documents_max_files == 3 with input as {"agent_role": "analyst"}
}

test_documents_max_files_elevated_for_developer if {
    proxy.documents_max_files == 10 with input as {"agent_role": "developer"}
}

test_documents_max_files_elevated_for_admin if {
    proxy.documents_max_files == 10 with input as {"agent_role": "admin"}
}

test_documents_max_files_zero_for_viewer if {
    proxy.documents_max_files == 0 with input as {"agent_role": "viewer"}
}

# ── pii_resolve_tool_results (enterprise) ────────────────────

test_pii_resolve_allowed_for_analyst_with_allowed_purpose if {
    proxy.pii_resolve_tool_results_allowed with input as {
        "agent_role": "analyst",
        "purpose": "support",
    }
}

test_pii_resolve_allowed_for_developer_with_billing_purpose if {
    proxy.pii_resolve_tool_results_allowed with input as {
        "agent_role": "developer",
        "purpose": "billing",
    }
}

test_pii_resolve_denied_for_viewer if {
    # viewer is not in proxy.pii_resolve_tool_results.allowed_roles
    not proxy.pii_resolve_tool_results_allowed with input as {
        "agent_role": "viewer",
        "purpose": "support",
    }
}

test_pii_resolve_denied_for_unknown_purpose if {
    # "marketing" is not in allowed_purposes — policy still has final say
    not proxy.pii_resolve_tool_results_allowed with input as {
        "agent_role": "analyst",
        "purpose": "marketing",
    }
}

test_pii_resolve_denied_when_no_purpose if {
    not proxy.pii_resolve_tool_results_allowed with input as {
        "agent_role": "analyst",
    }
}

test_pii_resolve_default_purpose_populated if {
    proxy.pii_resolve_tool_results_default_purpose == "support" with input as {}
}
