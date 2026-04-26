"""Tab C — The org behind the answer.

Renders the NovaTech org-chart that was seeded by ``scripts/seed_graph.py``.
Pick an employee, hit *Explore neighbourhood*, and an interactive graph
draws the person's department + project connections. Demonstrates that
the knowledge base carries structured context that pure vector search
cannot — good for "who do I need to talk to?" style questions.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

try:
    from streamlit_agraph import Config, Edge, Node, agraph
    _HAS_AGRAPH = True
except ImportError:  # pragma: no cover
    _HAS_AGRAPH = False

from mcp_client import _MCPClient


LABEL_COLORS = {
    "Employee":   "#2e7d32",
    "Department": "#1565c0",
    "Project":    "#c2185b",
}


def _extract_node(record: Any) -> dict[str, Any] | None:
    """Normalise a raw AGE node dict into {id, label, properties}."""
    if not isinstance(record, dict):
        return None
    if "label" in record and "properties" in record:
        return record  # already AGE vertex shape
    # find_node wraps it as {"n": <vertex>}
    if "n" in record:
        return _extract_node(record["n"])
    return None


def _list_employees(mcp: _MCPClient, api_key: str) -> list[dict]:
    try:
        raw = mcp.graph_query(
            "find_node",
            api_key=api_key,
            label="Employee",
            properties={},
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Cannot list employees: {exc}")
        return []

    nodes: list[dict] = []
    for item in raw.get("nodes") or []:
        node = _extract_node(item)
        if node and node.get("properties"):
            nodes.append(node["properties"])
    return sorted(nodes, key=lambda p: p.get("id", ""))


def _explore(mcp: _MCPClient, api_key: str, node_id: str, depth: int) -> dict:
    return mcp.graph_query(
        "get_neighbors",
        api_key=api_key,
        label="Employee",
        node_id=node_id,
        max_depth=depth,
        direction="both",
    )


def _flatten_rel(rel_raw: Any) -> list[dict]:
    """AGE variable-depth rels come back as list-of-edges. Normalise to list of dicts."""
    if rel_raw is None:
        return []
    if isinstance(rel_raw, list):
        flat = []
        for item in rel_raw:
            if isinstance(item, dict):
                flat.append(item)
            elif isinstance(item, list):
                flat.extend(e for e in item if isinstance(e, dict))
        return flat
    if isinstance(rel_raw, dict):
        return [rel_raw]
    return []


def _build_visualisation(root: dict, neighbours: list[dict]) -> tuple[list, list]:
    """Create Node/Edge lists for streamlit-agraph."""
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []

    def _add_node(props: dict, label: str) -> str | None:
        node_id = props.get("id")
        if node_id is None:
            return None
        if node_id not in nodes:
            color = LABEL_COLORS.get(label, "#607d8b")
            display = props.get("name") or str(node_id)
            nodes[node_id] = Node(
                id=str(node_id),
                label=f"{display}\n({label})",
                size=28 if label == "Employee" else 22,
                color=color,
                shape="dot",
            )
        return node_id

    # Root employee
    _add_node(root, "Employee")

    for row in neighbours:
        if not isinstance(row, dict):
            continue
        m_raw = row.get("m")
        r_raw = row.get("r")
        m = _extract_node(m_raw) if m_raw else None
        if m:
            _add_node(m.get("properties", {}), m.get("label", "Node"))

        for edge in _flatten_rel(r_raw):
            src = edge.get("start_id") or edge.get("start_vertex_id") or edge.get("start")
            dst = edge.get("end_id") or edge.get("end_vertex_id") or edge.get("end")
            rel_type = edge.get("label") or "REL"
            # AGE exposes start/end as internal integer vertex IDs (not our id property).
            # Fall back to scanning properties if the raw edge doesn't carry ours.
            props = edge.get("properties") or {}
            src_prop = props.get("from_id") or props.get("source_id") or src
            dst_prop = props.get("to_id") or props.get("target_id") or dst
            if src_prop is None or dst_prop is None:
                continue
            edges.append(
                Edge(
                    source=str(src_prop),
                    target=str(dst_prop),
                    label=rel_type,
                )
            )

    return list(nodes.values()), edges


def render(mcp: _MCPClient, analyst_key: str) -> None:
    st.subheader("The org behind the answer")
    st.write(
        "The knowledge graph captures structure that vector search ignores. "
        "Pick an employee from the NovaTech org-chart and we'll traverse the "
        "surrounding department and project relationships."
    )

    employees = _list_employees(mcp, analyst_key)

    if not employees:
        st.warning(
            "No employees found in the graph. Did the seed run? Try "
            "`docker compose --profile seed up seed` (or pass `--demo` to the quickstart)."
        )
        return

    labels = [f"{e.get('name', '?')} ({e.get('role', '')})" for e in employees]
    pick = st.selectbox(
        "Employee",
        options=list(range(len(employees))),
        format_func=lambda i: labels[i],
        index=0,
    )
    depth = st.slider("Traversal depth", min_value=1, max_value=3, value=2)

    chosen = employees[pick]
    st.caption(f"Root: {chosen.get('name', chosen.get('id'))} (`{chosen.get('id')}`)")

    try:
        result = _explore(mcp, analyst_key, chosen["id"], depth)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph traversal failed: {exc}")
        return

    neighbours = result.get("neighbors") or result.get("results") or []
    st.caption(f"{len(neighbours)} neighbour row(s) returned")

    # --- Visualisation or table fallback ------------------------------------
    if _HAS_AGRAPH:
        nodes, edges = _build_visualisation(chosen, neighbours)
        config = Config(
            width=900,
            height=520,
            directed=True,
            physics=True,
            hierarchical=False,
            nodeHighlightBehavior=True,
            highlightColor="#f9a825",
            collapsible=False,
        )
        agraph(nodes=nodes, edges=edges, config=config)
    else:
        st.info(
            "`streamlit-agraph` not installed — showing raw neighbour rows instead."
        )

    with st.expander("Raw graph_query response"):
        st.json(result, expanded=False)

    with st.expander("What this demonstrates"):
        st.markdown(
            """
            - Nodes and relationships live in Apache AGE inside PostgreSQL — no
              separate graph database to operate.
            - The MCP `graph_query` tool runs Cypher against AGE, sanitises all
              identifiers, and masks PII in returned properties (see B-30).
            - The seed we rendered here links `Employee → Department → Project`.
              In a real deployment these edges come from HR systems, project
              tooling, and source-control commit graphs.
            - An agent can ask follow-up questions like *"Who else is on the
              platform project?"* or *"Which department owns this codebase?"*
              without another vector search.
            """
        )
