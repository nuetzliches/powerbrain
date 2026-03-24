# ============================================================
#  Tests for pb.layers
# ============================================================

package pb.layers_test

import rego.v1
import data.pb.layers

test_analyst_l1_confidential_allowed if {
    layers.layer_allowed with input as {
        "agent_role": "analyst", "classification": "confidential", "requested_layer": "L1",
    }
}

test_analyst_l2_confidential_denied if {
    not layers.layer_allowed with input as {
        "agent_role": "analyst", "classification": "confidential", "requested_layer": "L2",
    }
}

test_admin_l2_confidential_allowed if {
    layers.layer_allowed with input as {
        "agent_role": "admin", "classification": "confidential", "requested_layer": "L2",
    }
}

test_viewer_l2_public_allowed if {
    layers.layer_allowed with input as {
        "agent_role": "viewer", "classification": "public", "requested_layer": "L2",
    }
}

test_viewer_l2_internal_denied if {
    not layers.layer_allowed with input as {
        "agent_role": "viewer", "classification": "internal", "requested_layer": "L2",
    }
}

test_max_layer_confidential_analyst if {
    layers.max_layer == "L1" with input as {
        "agent_role": "analyst", "classification": "confidential",
    }
}

test_max_layer_restricted_analyst if {
    layers.max_layer == "L0" with input as {
        "agent_role": "analyst", "classification": "restricted",
    }
}

test_max_layer_public_viewer if {
    layers.max_layer == "L2" with input as {
        "agent_role": "viewer", "classification": "public",
    }
}
