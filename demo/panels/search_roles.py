"""Tab A — Same question, different answers.

Runs the same search with two API keys (analyst vs. viewer) and shows
the side-by-side result set, classification badges, and rerank scores.
The contrast makes OPA policy enforcement visible to a non-engineering
audience in a single glance.
"""
from __future__ import annotations

import streamlit as st

from mcp_client import DemoOutOfDateError, SearchResponse, _MCPClient


CLASSIFICATION_COLORS = {
    "public":       "#2e7d32",  # green
    "internal":     "#1565c0",  # blue
    "confidential": "#ef6c00",  # orange
    "restricted":   "#c62828",  # red
}

SUGGESTED_QUERIES = [
    "Onboarding-Checkliste",
    "Homeoffice Regelung",
    "Kundenliste",
    "Gehaltsbänder",
]


def _badge(classification: str) -> str:
    color = CLASSIFICATION_COLORS.get(classification, "#555")
    return (
        f"<span style='background:{color};color:white;padding:2px 8px;"
        f"border-radius:10px;font-size:0.8em;'>{classification}</span>"
    )


def _render_column(label: str, role: str, response: SearchResponse | None, error: str | None) -> None:
    st.markdown(f"### {label}")
    st.caption(f"Role: `{role}`")

    if error:
        st.error(error)
        return

    if response is None:
        st.info("Run a query above to see results.")
        return

    st.caption(f"{response.total} result(s) returned by OPA filter + reranker")

    if response.total == 0:
        st.warning(
            "No results — either nothing matched, or every hit was filtered out "
            "by the access policy for this role."
        )
        return

    for i, item in enumerate(response.results, 1):
        classification = item.metadata.get("classification", "unknown")
        title = item.metadata.get("title") or item.metadata.get("seed_id") or f"Result {i}"
        with st.container(border=True):
            st.markdown(
                f"**{i}. {title}** &nbsp; {_badge(classification)}",
                unsafe_allow_html=True,
            )
            st.caption(
                f"Qdrant {item.score:.3f} · Reranker {item.rerank_score:.3f}"
            )
            snippet = (item.content or "").strip()
            if len(snippet) > 240:
                snippet = snippet[:240] + "…"
            if snippet:
                st.write(snippet)


def render(mcp: _MCPClient, analyst_key: str, viewer_key: str) -> None:
    st.subheader("Same question, different answers")
    st.write(
        "Two keys, two roles, one query. The search pipeline is identical — OPA "
        "policy and data classification decide what each role can read."
    )

    with st.form("search_form"):
        cols = st.columns([3, 1])
        with cols[0]:
            query = st.text_input(
                "Query",
                value=st.session_state.get("search_query", SUGGESTED_QUERIES[0]),
                key="search_query_input",
            )
        with cols[1]:
            top_k = st.number_input(
                "top_k", min_value=1, max_value=20, value=5, step=1,
            )
        st.write("Suggestions:")
        sug_cols = st.columns(len(SUGGESTED_QUERIES))
        for col, q in zip(sug_cols, SUGGESTED_QUERIES):
            with col:
                if st.form_submit_button(q):
                    query = q
                    st.session_state["search_query"] = q
        submitted = st.form_submit_button("Run query", type="primary")

    if not submitted and "search_last_query" not in st.session_state:
        st.info("Enter a query and press **Run query** to see the two roles' views.")
        return

    q = (query or "").strip()
    if submitted and q:
        st.session_state["search_last_query"] = q
        st.session_state["search_last_top_k"] = int(top_k)

    q = st.session_state.get("search_last_query", "")
    k = int(st.session_state.get("search_last_top_k", 5))
    if not q:
        return

    # Run both searches. Errors are isolated per-column so a single failure
    # doesn't black out the whole tab.
    analyst_resp, analyst_err = _safe_search(mcp, q, analyst_key, k)
    viewer_resp, viewer_err = _safe_search(mcp, q, viewer_key, k)

    left, right = st.columns(2, gap="large")
    with left:
        _render_column("Analyst", "analyst", analyst_resp, analyst_err)
    with right:
        _render_column("Viewer", "viewer", viewer_resp, viewer_err)

    with st.expander("What just happened?"):
        st.markdown(
            """
            1. **Both roles hit the same vector search** in Qdrant (top_k × 5 oversampling).
            2. **OPA evaluates** `pb.access.allow` on each hit using the role attached to the API key.
               The analyst may read `public`/`internal`/`confidential`; the viewer is limited to `public`.
            3. **Only allowed hits reach the reranker.** That is why the viewer column can be empty
               even when Qdrant returned matches — filtering happens *before* reranking, not after.
            4. **No agent-side role parameter exists.** The role is derived from the API key via
               the `api_keys` table, so a client cannot elevate its own privileges.
            """
        )


def _safe_search(
    mcp: _MCPClient,
    query: str,
    api_key: str,
    top_k: int,
) -> tuple[SearchResponse | None, str | None]:
    try:
        return mcp.search_knowledge(query, api_key=api_key, top_k=top_k), None
    except DemoOutOfDateError as exc:
        return None, f"Demo out of date — MCP response shape changed.\n\n{exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"Search failed: {exc}"
