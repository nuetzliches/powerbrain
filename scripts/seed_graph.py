#!/usr/bin/env python3
"""
Seed the Knowledge Graph with the NovaTech org-chart (for sales-demo).

Reads testdata/graph_seed.json and calls the `graph_mutate` MCP tool
(create_node / create_relationship). Idempotent: nodes and relationships
that already exist are skipped (via graph_query find_node / find_relationships).

Requires an API key with role `developer` or `admin`. By default uses the
hardcoded dev key; override via MCP_API_KEY env var.

Usage:
    python3 scripts/seed_graph.py
    MCP_URL=http://localhost:8080 MCP_API_KEY=pb_... python3 scripts/seed_graph.py

Environment variables:
    MCP_URL      — MCP server base URL (default: http://mcp-server:8080)
    MCP_API_KEY  — Bearer token for authentication (default: dev key)
    SEED_FILE    — Path to graph_seed.json (default: testdata/graph_seed.json next to this script)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MCP_URL = os.environ.get("MCP_URL", "http://mcp-server:8080")
MCP_API_KEY = os.environ.get(
    "MCP_API_KEY",
    "pb_dev_localonly_do_not_use_in_production",
)
def _default_seed_file() -> Path:
    """Locate graph_seed.json in either the repo layout or the flat /seed container layout."""
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "testdata" / "graph_seed.json",  # repo: scripts/ → testdata/
        here / "graph_seed.json",                       # container: /seed/ (flat)
        Path("/seed") / "graph_seed.json",              # explicit fallback
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]  # return first candidate so error message is informative


SEED_FILE = Path(os.environ.get("SEED_FILE", str(_default_seed_file())))

MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "120"))
POLL_INTERVAL = 3


# ── JSON-RPC helpers ─────────────────────────────────────────────────────────


def _rpc(method: str, params: dict, request_id: int = 1) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{MCP_URL}/mcp",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {MCP_API_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def _initialize() -> None:
    _rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "seed-graph", "version": "1.0"},
    }, request_id=0)


def _call_tool(name: str, arguments: dict, request_id: int = 1) -> dict:
    """Call an MCP tool and return the parsed JSON result (unwrapped from content[0].text)."""
    resp = _rpc("tools/call", {"name": name, "arguments": arguments}, request_id=request_id)
    if "error" in resp:
        raise RuntimeError(f"MCP error calling {name}: {resp['error']}")
    try:
        text = resp["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unexpected MCP response shape for {name}: {exc}: {resp}")


def _wait_for_mcp() -> None:
    print(f"Waiting for MCP at {MCP_URL} ...")
    deadline = time.time() + MAX_WAIT_SECONDS
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            _initialize()
            print("  MCP server: ready")
            return
        except Exception as exc:
            last_err = exc
            time.sleep(POLL_INTERVAL)
    print(f"  MCP server: TIMEOUT ({last_err})", file=sys.stderr)
    sys.exit(1)


# ── Idempotent operations ───────────────────────────────────────────────────


def _node_exists(label: str, node_id: str) -> bool:
    result = _call_tool("graph_query", {
        "action": "find_node",
        "label": label,
        "properties": {"id": node_id},
    })
    return (result.get("count") or 0) > 0


def _ensure_node(label: str, properties: dict) -> str:
    node_id = properties["id"]
    if _node_exists(label, node_id):
        return "skip"
    result = _call_tool("graph_mutate", {
        "action": "create_node",
        "label": label,
        "properties": properties,
    })
    if "error" in result:
        raise RuntimeError(f"create_node failed for {label}:{node_id} — {result['error']}")
    return "create"


def _relationship_exists(
    from_label: str, from_id: str,
    to_label: str, to_id: str,
    rel_type: str,
) -> bool:
    result = _call_tool("graph_query", {
        "action": "find_relationships",
        "from_label": from_label,
        "node_id": from_id,
        "rel_type": rel_type,
        "to_label": to_label,
        "to_id": to_id,
    })
    return (result.get("count") or 0) > 0


def _ensure_relationship(rel: dict) -> str:
    if _relationship_exists(
        rel["from_label"], rel["from_id"],
        rel["to_label"], rel["to_id"],
        rel["rel_type"],
    ):
        return "skip"
    args = {
        "action": "create_relationship",
        "from_label": rel["from_label"],
        "from_id": rel["from_id"],
        "to_label": rel["to_label"],
        "to_id": rel["to_id"],
        "rel_type": rel["rel_type"],
    }
    if rel.get("properties"):
        args["rel_properties"] = rel["properties"]
    result = _call_tool("graph_mutate", args)
    if "error" in result:
        raise RuntimeError(
            f"create_relationship failed for "
            f"{rel['from_label']}:{rel['from_id']} -[{rel['rel_type']}]-> "
            f"{rel['to_label']}:{rel['to_id']} — {result['error']}"
        )
    return "create"


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    if not SEED_FILE.exists():
        print(f"ERROR: seed file not found: {SEED_FILE}", file=sys.stderr)
        return 1

    with SEED_FILE.open() as f:
        seed = json.load(f)

    nodes = seed.get("nodes", [])
    relationships = seed.get("relationships", [])

    print(f"Graph Seed: {len(nodes)} nodes, {len(relationships)} relationships")
    print(f"  MCP:  {MCP_URL}")
    print(f"  File: {SEED_FILE}")
    print()

    _wait_for_mcp()

    created_nodes = skipped_nodes = 0
    for node in nodes:
        try:
            action = _ensure_node(node["label"], node["properties"])
            if action == "create":
                created_nodes += 1
                print(f"  + node  {node['label']:12s}  {node['properties']['id']}")
            else:
                skipped_nodes += 1
        except Exception as exc:
            print(f"  ! node  {node['label']}:{node['properties']['id']} — {exc}", file=sys.stderr)
            return 1

    created_rels = skipped_rels = 0
    for rel in relationships:
        try:
            action = _ensure_relationship(rel)
            if action == "create":
                created_rels += 1
                print(
                    f"  + edge  {rel['from_label']}:{rel['from_id']} "
                    f"-[{rel['rel_type']}]-> "
                    f"{rel['to_label']}:{rel['to_id']}"
                )
            else:
                skipped_rels += 1
        except Exception as exc:
            print(
                f"  ! edge  {rel['from_label']}:{rel['from_id']} "
                f"-[{rel['rel_type']}]-> "
                f"{rel['to_label']}:{rel['to_id']} — {exc}",
                file=sys.stderr,
            )
            return 1

    print()
    print(
        f"Nodes:         {created_nodes} created, {skipped_nodes} skipped (already present)"
    )
    print(
        f"Relationships: {created_rels} created, {skipped_rels} skipped (already present)"
    )
    print("Graph seed completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
