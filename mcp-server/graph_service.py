"""
Knowledge Graph Service
========================
Verwaltet den Apache AGE Knowledge Graph innerhalb von PostgreSQL.
Bietet Funktionen zum Erstellen, Abfragen und Traversieren von
Entitäten und Beziehungen.

Knotentypen (Vertex Labels):
  - Project      — Projekte mit Status und Klassifizierung
  - Technology   — Technologien, Frameworks, Tools
  - Person       — Personen, Teams, Rollen
  - Document     — Dokumente, Datensätze (Referenz zu PG/Qdrant)
  - Rule         — Business Rules, Policies
  - DataSource   — Datenquellen (Repos, APIs, Dateien)

Beziehungstypen (Edge Labels):
  - USES         — Project/Person → Technology
  - OWNS         — Person → Project/Document
  - DEPENDS_ON   — Project → Project, Technology → Technology
  - DOCUMENTS    — Document → Project/Technology
  - GOVERNS      — Rule → Project/DataSource
  - SOURCED_FROM — Document/DataSource → DataSource

Abhängigkeiten:
  asyncpg (bereits im MCP-Server vorhanden)
"""

import json
import logging
import re
from typing import Any

import asyncpg

log = logging.getLogger("kb-graph")

# ── Identifier-Validierung (Injection Prevention) ───────────

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_identifier(name: str) -> bool:
    """
    Prüft ob ein String ein sicherer Identifier ist (SQL-Spaltenname,
    Cypher-Label, Property-Key). Erlaubt nur ASCII-Buchstaben, Ziffern
    und Unterstriche; muss mit Buchstabe oder Unterstrich beginnen.

    Verhindert SQL-Injection (P1-2) und Cypher-Injection (P1-3).
    """
    if not isinstance(name, str) or not name:
        return False
    return bool(_IDENTIFIER_RE.match(name))


def _require_identifier(name: str, context: str = "identifier") -> None:
    """Wirft ValueError wenn name kein gültiger Identifier ist."""
    if not validate_identifier(name):
        raise ValueError(f"Ungültiger {context}: {name!r}")


# ── Cypher-Query-Hilfsfunktionen ────────────────────────────

GRAPH_NAME = "knowledge"

# AGE benötigt diese Initialisierung pro Connection
AGE_INIT = """
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
"""


async def _execute_cypher(pool: asyncpg.Pool, cypher: str, params: dict | None = None) -> list[dict]:
    """
    Führt eine Cypher-Query über Apache AGE aus.
    Gibt die Ergebnisse als Liste von Dicts zurück.
    """
    # Parameter in die Cypher-Query einsetzen (einfache String-Substitution)
    # AGE unterstützt keine parametrisierten Cypher-Queries wie Neo4j,
    # daher müssen wir sicher escapen.
    query = cypher
    if params:
        for key, value in params.items():
            safe_value = _escape_cypher_value(value)
            query = query.replace(f"${key}", safe_value)

    sql = f"""
    SELECT * FROM cypher('{GRAPH_NAME}', $$
        {query}
    $$) AS (result agtype)
    """

    async with pool.acquire() as conn:
        await conn.execute(AGE_INIT)
        rows = await conn.fetch(sql)

    results = []
    for row in rows:
        raw = row["result"]
        # agtype kommt als String, der JSON-kompatibel ist
        if raw is not None:
            try:
                parsed = json.loads(str(raw))
                results.append(parsed)
            except (json.JSONDecodeError, TypeError):
                results.append({"raw": str(raw)})

    return results


def _escape_cypher_value(value: Any) -> str:
    """Escaped einen Wert für die Einbettung in Cypher-Queries."""
    if isinstance(value, str):
        escaped = value.replace("'", "\\'").replace('"', '\\"')
        return f"'{escaped}'"
    elif isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, list):
        items = ", ".join(_escape_cypher_value(v) for v in value)
        return f"[{items}]"
    else:
        return f"'{str(value)}'"


# ── Knoten-Operationen ──────────────────────────────────────

async def create_node(pool: asyncpg.Pool, label: str, properties: dict) -> dict:
    """Erstellt einen Knoten im Graph."""
    _require_identifier(label, "Label")
    for k in properties:
        _require_identifier(k, "Property-Key")
    props_str = ", ".join(f"{k}: {_escape_cypher_value(v)}" for k, v in properties.items())
    cypher = f"CREATE (n:{label} {{{props_str}}}) RETURN n"
    results = await _execute_cypher(pool, cypher)

    # Sync-Log
    node_id = properties.get("id", properties.get("name", "unknown"))
    await _log_sync(pool, label.lower(), str(node_id), "create")

    return results[0] if results else {}


async def find_node(pool: asyncpg.Pool, label: str, properties: dict) -> list[dict]:
    """Findet Knoten nach Label und Properties."""
    _require_identifier(label, "Label")
    where_parts = []
    for k, v in properties.items():
        _require_identifier(k, "Property-Key")
        where_parts.append(f"n.{k} = {_escape_cypher_value(v)}")
    where_clause = " AND ".join(where_parts) if where_parts else "true"

    cypher = f"MATCH (n:{label}) WHERE {where_clause} RETURN n"
    return await _execute_cypher(pool, cypher)


async def delete_node(pool: asyncpg.Pool, label: str, node_id: str) -> bool:
    """Löscht einen Knoten und alle seine Beziehungen."""
    _require_identifier(label, "Label")
    cypher = f"MATCH (n:{label} {{id: {_escape_cypher_value(node_id)}}}) DETACH DELETE n"
    await _execute_cypher(pool, cypher)
    await _log_sync(pool, label.lower(), node_id, "delete")
    return True


# ── Beziehungs-Operationen ──────────────────────────────────

async def create_relationship(
    pool: asyncpg.Pool,
    from_label: str, from_id: str,
    to_label: str, to_id: str,
    rel_type: str,
    properties: dict | None = None,
) -> dict:
    """Erstellt eine Beziehung zwischen zwei Knoten."""
    _require_identifier(from_label, "from_label")
    _require_identifier(to_label, "to_label")
    _require_identifier(rel_type, "rel_type")
    props_str = ""
    if properties:
        for k in properties:
            _require_identifier(k, "Property-Key")
        props_items = ", ".join(f"{k}: {_escape_cypher_value(v)}" for k, v in properties.items())
        props_str = f" {{{props_items}}}"

    cypher = (
        f"MATCH (a:{from_label} {{id: {_escape_cypher_value(from_id)}}}), "
        f"(b:{to_label} {{id: {_escape_cypher_value(to_id)}}}) "
        f"CREATE (a)-[r:{rel_type}{props_str}]->(b) RETURN r"
    )
    results = await _execute_cypher(pool, cypher)
    return results[0] if results else {}


async def find_relationships(
    pool: asyncpg.Pool,
    from_label: str | None = None,
    from_id: str | None = None,
    rel_type: str | None = None,
    to_label: str | None = None,
    to_id: str | None = None,
    depth: int = 1,
) -> list[dict]:
    """
    Findet Beziehungen im Graph.
    Unterstützt variable Tiefe für Pfad-Traversierung.
    """
    if from_label:
        _require_identifier(from_label, "from_label")
    if to_label:
        _require_identifier(to_label, "to_label")
    if rel_type:
        _require_identifier(rel_type, "rel_type")
    # Dynamisch die MATCH-Klausel aufbauen
    from_part = f"(a:{from_label}" if from_label else "(a"
    if from_id:
        from_part += f" {{id: {_escape_cypher_value(from_id)}}}"
    from_part += ")"

    to_part = f"(b:{to_label}" if to_label else "(b"
    if to_id:
        to_part += f" {{id: {_escape_cypher_value(to_id)}}}"
    to_part += ")"

    rel_part = f"[r:{rel_type}]" if rel_type else "[r]"

    if depth > 1:
        rel_part = f"[r:{rel_type}*1..{depth}]" if rel_type else f"[r*1..{depth}]"

    cypher = f"MATCH {from_part}-{rel_part}->{to_part} RETURN a, r, b"
    return await _execute_cypher(pool, cypher)


# ── Graph-Traversierung ────────────────────────────────────

async def get_neighbors(pool: asyncpg.Pool, label: str, node_id: str,
                        direction: str = "both", max_depth: int = 1) -> list[dict]:
    """
    Findet alle Nachbarn eines Knotens.
    direction: 'out', 'in', oder 'both'
    """
    _require_identifier(label, "Label")
    node_match = f"(n:{label} {{id: {_escape_cypher_value(node_id)}}})"

    if direction == "out":
        cypher = f"MATCH {node_match}-[r*1..{max_depth}]->(m) RETURN DISTINCT m, r"
    elif direction == "in":
        cypher = f"MATCH {node_match}<-[r*1..{max_depth}]-(m) RETURN DISTINCT m, r"
    else:
        cypher = f"MATCH {node_match}-[r*1..{max_depth}]-(m) RETURN DISTINCT m, r"

    return await _execute_cypher(pool, cypher)


async def find_path(pool: asyncpg.Pool,
                    from_label: str, from_id: str,
                    to_label: str, to_id: str,
                    max_depth: int = 5) -> list[dict]:
    """Findet den kürzesten Pfad zwischen zwei Knoten."""
    _require_identifier(from_label, "from_label")
    _require_identifier(to_label, "to_label")
    cypher = (
        f"MATCH p = shortestPath("
        f"(a:{from_label} {{id: {_escape_cypher_value(from_id)}}})-[*..{max_depth}]-"
        f"(b:{to_label} {{id: {_escape_cypher_value(to_id)}}}))"
        f" RETURN p"
    )
    return await _execute_cypher(pool, cypher)


async def get_subgraph(pool: asyncpg.Pool, label: str, node_id: str,
                       max_depth: int = 2) -> dict:
    """
    Gibt einen Subgraph um einen Knoten zurück.
    Nützlich für Kontext-Anreicherung bei MCP-Queries.
    """
    nodes = await get_neighbors(pool, label, node_id, direction="both", max_depth=max_depth)

    # Zentralen Knoten hinzufügen
    center = await find_node(pool, label, {"id": node_id})

    return {
        "center": center[0] if center else None,
        "neighbors": nodes,
        "depth": max_depth,
    }


# ── Bulk-Import ─────────────────────────────────────────────

async def sync_project_to_graph(pool: asyncpg.Pool, project: dict) -> None:
    """Synchronisiert ein Projekt aus PostgreSQL in den Knowledge Graph."""
    # Knoten erstellen/aktualisieren (MERGE-Semantik)
    cypher = (
        f"MERGE (p:Project {{id: {_escape_cypher_value(project['id'])}}}) "
        f"SET p.name = {_escape_cypher_value(project.get('name', ''))}, "
        f"    p.status = {_escape_cypher_value(project.get('status', 'active'))}, "
        f"    p.classification = {_escape_cypher_value(project.get('classification', 'internal'))}"
        f" RETURN p"
    )
    await _execute_cypher(pool, cypher)
    await _log_sync(pool, "project", project["id"], "update")


async def sync_dataset_to_graph(pool: asyncpg.Pool, dataset: dict) -> None:
    """Synchronisiert einen Datensatz als Document-Knoten."""
    cypher = (
        f"MERGE (d:Document {{id: {_escape_cypher_value(str(dataset['id']))}}}) "
        f"SET d.name = {_escape_cypher_value(dataset.get('name', ''))}, "
        f"    d.source_type = {_escape_cypher_value(dataset.get('source_type', ''))}, "
        f"    d.classification = {_escape_cypher_value(dataset.get('classification', 'internal'))}"
        f" RETURN d"
    )
    await _execute_cypher(pool, cypher)

    # Beziehung zu Projekt herstellen, wenn vorhanden
    if dataset.get("project"):
        link_cypher = (
            f"MATCH (d:Document {{id: {_escape_cypher_value(str(dataset['id']))}}}), "
            f"(p:Project {{id: {_escape_cypher_value(dataset['project'])}}}) "
            f"MERGE (d)-[:DOCUMENTS]->(p)"
            f" RETURN d, p"
        )
        await _execute_cypher(pool, link_cypher)

    await _log_sync(pool, "document", str(dataset["id"]), "update")


# ── Logging ─────────────────────────────────────────────────

async def _log_sync(pool: asyncpg.Pool, entity_type: str, entity_id: str, action: str):
    """Protokolliert eine Graph-Synchronisation."""
    await pool.execute(
        "INSERT INTO graph_sync_log (entity_type, entity_id, action) VALUES ($1, $2, $3)",
        entity_type, entity_id, action,
    )
