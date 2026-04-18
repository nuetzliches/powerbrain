"""Powerbrain Sales-Demo UI.

Three-tab Streamlit app that showcases the three most-asked-about
capabilities in the same session: role-based OPA filtering, the PII vault,
and the knowledge graph. Pointed at a running Powerbrain stack via the
MCP_URL environment variable.

Start via:  docker compose --profile demo up pb-demo
"""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from mcp_client import get_clients
from panels import knowledge_graph, pii_vault, search_roles

# ─── Page setup ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Powerbrain — Sales Demo",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

ASSETS_DIR = Path(__file__).parent / "assets"
DEMO_ANALYST_KEY = os.environ.get("DEMO_ANALYST_KEY", "pb_demo_analyst_localonly")
DEMO_VIEWER_KEY = os.environ.get("DEMO_VIEWER_KEY", "pb_demo_viewer_localonly")
DEV_ADMIN_KEY = os.environ.get("DEV_ADMIN_KEY", "pb_dev_localonly_do_not_use_in_production")


# ─── Sidebar ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## Powerbrain Demo")
    st.caption("Policy-aware context engine for enterprise AI")

    mcp_url = os.environ.get("MCP_URL", "http://localhost:8080")
    ingestion_url = os.environ.get("INGESTION_URL", "http://localhost:8081")
    st.markdown(
        f"""
        **Backend**
        - MCP: `{mcp_url}`
        - Ingestion: `{ingestion_url}`
        """
    )

    st.markdown("---")
    st.markdown("### Demo API keys")
    st.caption("Analyst vs. Viewer shows OPA filtering in Tab A.")
    st.code(
        f"analyst → {DEMO_ANALYST_KEY}\nviewer  → {DEMO_VIEWER_KEY}",
        language="text",
    )

    st.markdown("---")
    talk_track_file = ASSETS_DIR / "talk_track.md"
    if talk_track_file.exists():
        with st.expander("Presenter talk-track", expanded=False):
            st.markdown(talk_track_file.read_text(encoding="utf-8"))

    st.markdown("---")
    st.caption(
        "Not for production use. The pre-seeded demo keys are checked into the "
        "repo and must never be exposed on a public network."
    )


# ─── Main ───────────────────────────────────────────────────────────────────

mcp, ingestion = get_clients()

# Warm the session — Streamlit reruns on interaction, but a single init call
# per-session is enough.
if "mcp_initialized" not in st.session_state:
    try:
        mcp.initialize(api_key=DEV_ADMIN_KEY)
        st.session_state["mcp_initialized"] = True
    except Exception as exc:
        st.error(
            f"Cannot reach the MCP server at {mcp.url}. Start the stack first:\n\n"
            f"`./scripts/quickstart.sh --demo`\n\nDetails: {exc}"
        )
        st.stop()

st.title("Powerbrain — Sales Demo")
st.write(
    "Three capabilities that differentiate Powerbrain from a naive RAG setup. "
    "Each tab runs against the same live instance — no mock data."
)

tab_search, tab_pii, tab_graph = st.tabs([
    "A · Same question, different answers",
    "B · We never stored the secret",
    "C · The org behind the answer",
])

with tab_search:
    search_roles.render(mcp, DEMO_ANALYST_KEY, DEMO_VIEWER_KEY)

with tab_pii:
    pii_vault.render(mcp, ingestion, DEMO_ANALYST_KEY, DEV_ADMIN_KEY)

with tab_graph:
    knowledge_graph.render(mcp, DEMO_ANALYST_KEY)
