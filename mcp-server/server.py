"""
Wissensdatenbank MCP-Server
============================
Einziger Zugangspunkt für Agenten. Implementiert MCP-Tools für
semantische Suche, strukturierte Abfragen, Regelwerk-Zugriff,
Daten-Ingestion, Evaluation/Feedback und Snapshots.

Baustein 3: submit_feedback, get_eval_stats (+ Feedback-Loop in search_knowledge)
Baustein 4: create_snapshot, list_snapshots
Baustein 5: Prometheus-Metriken (/metrics HTTP auf Port 8080) + OpenTelemetry Tracing
"""

import os
import json
import logging
import time
from typing import Any

import httpx
import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent
from mcp.server.auth.provider import TokenVerifier, AccessToken
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Scope, Receive, Send
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from prometheus_client import (
    Counter, Histogram, Gauge,
    start_http_server as prom_start_http_server,
)
import uvicorn
import hashlib

import graph_service as graph
from graph_service import validate_identifier

# ── Konfiguration ────────────────────────────────────────────
QDRANT_URL    = os.getenv("QDRANT_URL",    "http://localhost:6333")
POSTGRES_URL  = os.getenv("POSTGRES_URL",  "postgresql://kb_admin:changeme@localhost:5432/knowledgebase")
OPA_URL       = os.getenv("OPA_URL",       "http://localhost:8181")
OLLAMA_URL    = os.getenv("OLLAMA_URL",    "http://localhost:11434")
FORGEJO_URL   = os.getenv("FORGEJO_URL",   "http://forgejo.local:3000")
FORGEJO_TOKEN = os.getenv("FORGEJO_TOKEN", "")
RERANKER_URL  = os.getenv("RERANKER_URL",  "http://reranker:8082")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() == "true"
INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")

MCP_HOST       = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT       = int(os.getenv("MCP_PORT", "8080"))
MCP_PATH       = os.getenv("MCP_PATH", "/mcp")
METRICS_PORT   = int(os.getenv("METRICS_PORT", "9091"))
OTEL_ENABLED   = os.getenv("OTEL_ENABLED", "false").lower() == "true"
OTLP_ENDPOINT  = os.getenv("OTLP_ENDPOINT", "http://tempo:4317")
AUTH_REQUIRED  = os.getenv("AUTH_REQUIRED", "true").lower() == "true"

EMBEDDING_MODEL    = "nomic-embed-text"
DEFAULT_TOP_K      = 10
OVERSAMPLE_FACTOR  = 5

# Feedback-Loop: Warnung wenn avg_rating unter diesem Schwellwert mit mind. N Feedbacks
FEEDBACK_WARN_THRESHOLD = 2.5
FEEDBACK_WARN_MIN_COUNT = 3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kb-mcp")

# ── Prometheus Metriken ──────────────────────────────────────
mcp_requests_total = Counter(
    "kb_mcp_requests_total",
    "MCP-Requests pro Tool und Status",
    ["tool", "status"],
)
mcp_request_duration = Histogram(
    "kb_mcp_request_duration_seconds",
    "Latenz pro MCP-Tool",
    ["tool"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)
mcp_policy_decisions_total = Counter(
    "kb_mcp_policy_decisions_total",
    "OPA Policy-Entscheidungen",
    ["result"],
)
mcp_search_results_count = Histogram(
    "kb_mcp_search_results_count",
    "Anzahl Suchergebnisse nach Reranking",
    ["collection"],
    buckets=[0, 1, 3, 5, 10, 20, 50],
)
mcp_rerank_fallback_total = Counter(
    "kb_mcp_rerank_fallback_total",
    "Anzahl Reranker-Fallbacks (nicht erreichbar)",
)
mcp_feedback_avg_rating = Gauge(
    "kb_feedback_avg_rating",
    "Aktueller Durchschnitt des Feedback-Ratings (letzte 24h)",
)

# ── OpenTelemetry Setup ──────────────────────────────────────
tracer = None
if OTEL_ENABLED:
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT)))
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer("kb-mcp-server")
        log.info(f"OpenTelemetry Tracing aktiviert → {OTLP_ENDPOINT}")
    except ImportError:
        log.warning("opentelemetry-* Pakete nicht installiert, Tracing deaktiviert")


def _otel_span(name: str):
    """Context-Manager für einen OTel-Span, no-op wenn Tracing deaktiviert."""
    if tracer:
        return tracer.start_as_current_span(name)
    from contextlib import nullcontext
    return nullcontext()


# ── Clients ──────────────────────────────────────────────────
qdrant  = AsyncQdrantClient(url=QDRANT_URL)
http    = httpx.AsyncClient(timeout=30.0)
pg_pool: asyncpg.Pool | None = None


async def get_pg_pool() -> asyncpg.Pool:
    global pg_pool
    if pg_pool is None:
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
    return pg_pool


# ── API-Key-Authentifizierung ────────────────────────────────

class ApiKeyVerifier(TokenVerifier):
    """TokenVerifier implementation that validates API keys against PostgreSQL."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        pool = await get_pg_pool()
        row = await pool.fetchrow(
            "SELECT agent_id, agent_role FROM api_keys "
            "WHERE key_hash = $1 AND active = true "
            "AND (expires_at IS NULL OR expires_at > now())",
            key_hash,
        )
        if row is None:
            return None
        # Update last_used_at (fire-and-forget, don't block auth)
        try:
            await pool.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE key_hash = $1",
                key_hash,
            )
        except Exception:
            pass  # Non-critical, don't fail auth over this
        return AccessToken(
            token=token,
            client_id=row["agent_id"],
            scopes=[row["agent_role"]],
        )


# ── Hilfsfunktionen ──────────────────────────────────────────

async def embed_text(text: str) -> list[float]:
    with _otel_span("embed_text"):
        resp = await http.post(f"{OLLAMA_URL}/api/embed", json={
            "model": EMBEDDING_MODEL, "input": text
        })
        resp.raise_for_status()
        return resp.json()["embeddings"][0]


async def rerank_results(query: str, documents: list[dict], top_n: int) -> list[dict]:
    if not RERANKER_ENABLED or not documents:
        return documents[:top_n]

    with _otel_span("rerank"):
        try:
            resp = await http.post(f"{RERANKER_URL}/rerank", json={
                "query": query,
                "documents": [
                    {"id": doc["id"], "content": doc["content"],
                     "score": doc.get("score", 0.0), "metadata": doc.get("metadata", {})}
                    for doc in documents
                ],
                "top_n": top_n,
                "return_scores": True,
            })
            resp.raise_for_status()
            data = resp.json()
            return [
                {"id": r["id"], "score": r["original_score"], "rerank_score": r["rerank_score"],
                 "rank": r["rank"], "content": r["content"], "metadata": r["metadata"]}
                for r in data["results"]
            ]
        except Exception as e:
            log.warning(f"Reranker nicht erreichbar, nutze Original-Reihenfolge: {e}")
            mcp_rerank_fallback_total.inc()
            return documents[:top_n]


async def check_opa_policy(agent_id: str, agent_role: str,
                           resource: str, classification: str,
                           action: str = "read") -> dict:
    with _otel_span("opa_check"):
        input_data = {
            "agent_id": agent_id, "agent_role": agent_role,
            "resource": resource, "classification": classification, "action": action,
        }
        try:
            resp = await http.post(
                f"{OPA_URL}/v1/data/kb/access/allow", json={"input": input_data}
            )
            resp.raise_for_status()
            allowed = resp.json().get("result", False)
        except Exception as e:
            log.warning(f"OPA check failed, defaulting to deny: {e}")
            allowed = False

        mcp_policy_decisions_total.labels(result="allow" if allowed else "deny").inc()
        return {"allowed": allowed, "input": input_data}


async def log_access(agent_id: str, agent_role: str,
                     resource_type: str, resource_id: str,
                     action: str, policy_result: str,
                     context: dict | None = None):
    contains_pii = False

    if context and "query" in context:
        # Scan query text for PII before storing
        scan_resp = await http.post(f"{INGESTION_URL}/scan", json={
            "text": context["query"],
        })
        scan_resp.raise_for_status()
        scan_data = scan_resp.json()

        contains_pii = scan_data["contains_pii"]
        context["query"] = scan_data["masked_text"]
        if contains_pii:
            context["query_contains_pii"] = True
            context["pii_entity_types"] = scan_data["entity_types"]

    pool = await get_pg_pool()
    await pool.execute("""
        INSERT INTO agent_access_log
            (agent_id, agent_role, resource_type, resource_id,
             action, policy_result, request_context, contains_pii)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    """, agent_id, agent_role, resource_type, resource_id,
       action, policy_result, json.dumps(context or {}), contains_pii)


# ── Vault Access ────────────────────────────────────────────

VAULT_HMAC_SECRET = os.getenv("VAULT_HMAC_SECRET", "change-me-in-production")


def validate_pii_access_token(token: dict) -> dict:
    """
    Validiert einen PII Access Token (HMAC-signiert, kurzlebig).
    Returns: {"valid": bool, "reason": str, "payload": dict}
    """
    import hmac as hmac_mod
    import hashlib
    from datetime import datetime, timezone

    signature = token.get("signature", "")
    payload = {k: v for k, v in token.items() if k != "signature"}

    # HMAC-Signatur prüfen
    expected = hmac_mod.new(
        VAULT_HMAC_SECRET.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac_mod.compare_digest(signature, expected):
        return {"valid": False, "reason": "Invalid token signature", "payload": payload}

    # Ablauf prüfen
    expires_at = token.get("expires_at", "")
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            return {"valid": False, "reason": "Token expired", "payload": payload}
    except (ValueError, TypeError):
        return {"valid": False, "reason": "Invalid expires_at format", "payload": payload}

    return {"valid": True, "reason": "ok", "payload": payload}


async def check_opa_vault_access(
    agent_role: str, purpose: str, classification: str,
    data_category: str, token_valid: bool, token_expired: bool,
) -> dict:
    """Prüft via OPA ob Vault-Zugriff erlaubt ist."""
    input_data = {
        "agent_role": agent_role,
        "purpose": purpose,
        "classification": classification,
        "data_category": data_category,
        "token_valid": token_valid,
        "token_expired": token_expired,
    }
    try:
        resp = await http.post(
            f"{OPA_URL}/v1/data/kb/privacy/vault_access_allowed",
            json={"input": input_data},
        )
        resp.raise_for_status()
        allowed = resp.json().get("result", False)
        fields_resp = await http.post(
            f"{OPA_URL}/v1/data/kb/privacy/vault_fields_to_redact",
            json={"input": input_data},
        )
        fields_resp.raise_for_status()
        fields_to_redact = list(fields_resp.json().get("result", []))
    except Exception as e:
        log.warning(f"OPA vault access check failed: {e}")
        allowed = False
        fields_to_redact = []
    return {
        "allowed": allowed,
        "fields_to_redact": fields_to_redact,
    }


def redact_fields(text: str, pii_entities: list[dict], fields_to_redact: set[str]) -> str:
    """Redaktiert bestimmte PII-Entity-Typen im Text basierend auf OPA-Policy."""
    # Mapping von OPA-Feldnamen zu Presidio-Entity-Typen
    field_to_entity = {
        "email": "EMAIL_ADDRESS",
        "phone": "PHONE_NUMBER",
        "iban": "IBAN_CODE",
        "birthdate": "DATE_OF_BIRTH",
        "address": "LOCATION",
    }
    entities_to_redact = {
        field_to_entity[f] for f in fields_to_redact if f in field_to_entity
    }

    if not entities_to_redact:
        return text

    # Sortiere nach Position absteigend für stabile Offsets
    sorted_entities = sorted(pii_entities, key=lambda e: e.get("start", 0), reverse=True)
    result = text
    for entity in sorted_entities:
        if entity.get("type") in entities_to_redact:
            start = entity.get("start", 0)
            end = entity.get("end", 0)
            if 0 <= start < end <= len(result):
                result = result[:start] + f"<{entity['type']}>" + result[end:]
    return result


async def vault_lookup(
    document_id: str, chunk_indices: list[int] | None = None
) -> list[dict]:
    """Holt Original-Daten aus dem Vault."""
    pool = await get_pg_pool()
    if chunk_indices:
        rows = await pool.fetch("""
            SELECT id, chunk_index, original_text, pii_entities
            FROM pii_vault.original_content
            WHERE document_id = $1 AND chunk_index = ANY($2)
            ORDER BY chunk_index
        """, document_id, chunk_indices)
    else:
        rows = await pool.fetch("""
            SELECT id, chunk_index, original_text, pii_entities
            FROM pii_vault.original_content
            WHERE document_id = $1
            ORDER BY chunk_index
        """, document_id)
    return [
        {
            "vault_id": str(r["id"]),
            "chunk_index": r["chunk_index"],
            "original_text": r["original_text"],
            "pii_entities": json.loads(r["pii_entities"])
                if isinstance(r["pii_entities"], str)
                else r["pii_entities"],
        }
        for r in rows
    ]


async def log_vault_access(
    agent_id: str, document_id: str, chunk_index: int | None,
    purpose: str, token_hash: str,
):
    """Loggt Vault-Zugriff in separates Audit-Log."""
    pool = await get_pg_pool()
    await pool.execute("""
        INSERT INTO pii_vault.vault_access_log
            (agent_id, document_id, chunk_index, purpose, token_hash)
        VALUES ($1, $2, $3, $4, $5)
    """, agent_id, document_id, chunk_index, purpose, token_hash)


async def check_feedback_warning(query: str, pool: asyncpg.Pool):
    """Warnt wenn eine Query häufig schlecht bewertet wird (Feedback-Loop)."""
    row = await pool.fetchrow("""
        SELECT COUNT(*) AS cnt, AVG(rating) AS avg_rating
        FROM search_feedback
        WHERE query = $1
    """, query)
    if row and row["cnt"] >= FEEDBACK_WARN_MIN_COUNT:
        avg = float(row["avg_rating"])
        if avg < FEEDBACK_WARN_THRESHOLD:
            log.warning(
                f"[Feedback-Loop] Query '{query[:80]}' hat avg_rating={avg:.2f} "
                f"bei {row['cnt']} Feedbacks → Retrieval-Qualität prüfen"
            )


# ── MCP-Server ───────────────────────────────────────────────
server = Server("kb-mcp-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_knowledge",
            description="Semantische Suche über die Wissensdatenbank. "
                        "Findet relevante Dokumente, Code-Snippets und Regeln.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query":      {"type": "string"},
                    "collection": {"type": "string",
                                   "enum": ["knowledge_general", "knowledge_code", "knowledge_rules"],
                                   "default": "knowledge_general"},
                    "filters":    {"type": "object"},
                    "top_k":      {"type": "integer", "default": 10},
                    "pii_access_token": {
                        "type": "object",
                        "description": "Optional: HMAC-signed token for accessing original PII data from vault",
                    },
                    "purpose": {
                        "type": "string",
                        "description": "Required with pii_access_token: purpose for accessing PII data",
                    },
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="query_data",
            description="Strukturierte Abfrage auf PostgreSQL-Datensätze.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dataset":    {"type": "string"},
                    "conditions": {"type": "object"},
                    "limit":      {"type": "integer", "default": 50},
                },
                "required": ["dataset"]
            }
        ),
        Tool(
            name="get_rules",
            description="Aktive Business Rules für einen Kontext abrufen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category":   {"type": "string"},
                    "context":    {"type": "object"},
                },
                "required": ["category"]
            }
        ),
        Tool(
            name="check_policy",
            description="OPA-Policy evaluieren.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action":         {"type": "string"},
                    "resource":       {"type": "string"},
                    "classification": {"type": "string"},
                },
                "required": ["action", "resource", "classification"]
            }
        ),
        Tool(
            name="ingest_data",
            description="Neue Daten in die Wissensdatenbank einspeisen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source":         {"type": "string"},
                    "source_type":    {"type": "string", "enum": ["csv", "json", "sql_dump", "git_repo"]},
                    "project":        {"type": "string"},
                    "classification": {"type": "string", "default": "internal"},
                    "metadata":       {"type": "object"},
                },
                "required": ["source", "source_type"]
            }
        ),
        Tool(
            name="get_classification",
            description="Klassifizierung eines Datenobjekts abfragen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id":   {"type": "string"},
                    "resource_type": {"type": "string", "enum": ["dataset", "document", "rule"]},
                },
                "required": ["resource_id", "resource_type"]
            }
        ),
        Tool(
            name="list_datasets",
            description="Verfügbare Datensätze auflisten.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project":     {"type": "string"},
                    "source_type": {"type": "string"},
                },
                "required": []
            }
        ),
        Tool(
            name="get_code_context",
            description="Code-Kontext aus Repos abrufen. Semantische Suche über Code-Embeddings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query":      {"type": "string"},
                    "repo":       {"type": "string"},
                    "language":   {"type": "string"},
                    "top_k":      {"type": "integer", "default": 5},
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="graph_query",
            description="Knowledge Graph abfragen (Knoten, Beziehungen, Pfade).",
            inputSchema={
                "type": "object",
                "properties": {
                    "action":     {"type": "string",
                                   "enum": ["find_node", "find_relationships", "get_neighbors", "find_path", "get_subgraph"]},
                    "label":      {"type": "string"},
                    "node_id":    {"type": "string"},
                    "properties": {"type": "object"},
                    "rel_type":   {"type": "string"},
                    "to_label":   {"type": "string"},
                    "to_id":      {"type": "string"},
                    "max_depth":  {"type": "integer", "default": 2},
                    "direction":  {"type": "string", "enum": ["out", "in", "both"], "default": "both"},
                },
                "required": ["action"]
            }
        ),
        Tool(
            name="graph_mutate",
            description="Knowledge Graph verändern (nur developer/admin).",
            inputSchema={
                "type": "object",
                "properties": {
                    "action":         {"type": "string",
                                       "enum": ["create_node", "delete_node", "create_relationship"]},
                    "label":          {"type": "string"},
                    "node_id":        {"type": "string"},
                    "properties":     {"type": "object"},
                    "from_label":     {"type": "string"},
                    "from_id":        {"type": "string"},
                    "to_label":       {"type": "string"},
                    "to_id":          {"type": "string"},
                    "rel_type":       {"type": "string"},
                    "rel_properties": {"type": "object"},
                },
                "required": ["action"]
            }
        ),
        # ── Baustein 3: Evaluation + Feedback ──────────────────
        Tool(
            name="submit_feedback",
            description="Feedback zu Suchergebnissen einreichen. "
                        "Bewertet die Qualität einer Suche (1–5 Sterne).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query":          {"type": "string", "description": "Die ursprüngliche Suchanfrage"},
                    "result_ids":     {"type": "array", "items": {"type": "string"},
                                       "description": "IDs der erhaltenen Ergebnisse"},
                    "rating":         {"type": "integer", "minimum": 1, "maximum": 5,
                                       "description": "Gesamtbewertung (1=schlecht, 5=sehr gut)"},
                    "relevant_ids":   {"type": "array", "items": {"type": "string"},
                                       "description": "IDs der hilfreichen Ergebnisse"},
                    "irrelevant_ids": {"type": "array", "items": {"type": "string"},
                                       "description": "IDs der nicht hilfreichen Ergebnisse"},
                    "comment":        {"type": "string", "description": "Freitext-Kommentar"},
                    "collection":     {"type": "string"},
                    "rerank_scores":  {"type": "object"},
                },
                "required": ["query", "result_ids", "rating"]
            }
        ),
        Tool(
            name="get_eval_stats",
            description="Statistiken zur Retrieval-Qualität abrufen. "
                        "Zeigt avg_rating, schlechteste Queries und Trend.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days":       {"type": "integer", "default": 30,
                                   "description": "Auswertungszeitraum in Tagen"},
                },
                "required": []
            }
        ),
        # ── Baustein 4: Snapshots ───────────────────────────────
        Tool(
            name="create_snapshot",
            description="Wissens-Snapshot erstellen (Qdrant + PG + OPA Policy-Commit). "
                        "Nur für admin.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "Snapshot-Name (z.B. 'before-migration-v2')"},
                    "description": {"type": "string"},
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="list_snapshots",
            description="Verfügbare Wissens-Snapshots auflisten.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit":      {"type": "integer", "default": 10},
                },
                "required": []
            }
        ),
    ]


# ── Tool-Implementierungen ───────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # ── Identity from auth token (preferred) or arguments (legacy) ──
    access_token = get_access_token()
    if access_token is not None:
        agent_id = access_token.client_id
        agent_role = access_token.scopes[0] if access_token.scopes else "unknown"
    elif not AUTH_REQUIRED:
        # Legacy fallback: self-declared identity (only when auth is optional)
        agent_id = arguments.get("agent_id", "unknown")
        agent_role = arguments.get("agent_role", "unknown")
        log.warning("Unauthenticated request for tool '%s' from agent_id='%s'", name, agent_id)
    else:
        # Should not reach here (RequireAuthMiddleware already rejected)
        return [TextContent(type="text", text=json.dumps({"error": "authentication required"}))]

    t_start = time.perf_counter()
    status  = "ok"

    with _otel_span(f"mcp.{name}"):
        try:
            result = await _dispatch(name, arguments, agent_id, agent_role)
        except Exception as e:
            log.error(f"Tool {name} fehlgeschlagen: {e}", exc_info=True)
            status = "error"
            result = [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elapsed = time.perf_counter() - t_start
    mcp_requests_total.labels(tool=name, status=status).inc()
    mcp_request_duration.labels(tool=name).observe(elapsed)

    return result


async def _dispatch(name: str, arguments: dict[str, Any],
                    agent_id: str, agent_role: str) -> list[TextContent]:

    # ── search_knowledge ─────────────────────────────────────
    if name == "search_knowledge":
        collection = arguments.get("collection", "knowledge_general")
        query      = arguments["query"]
        top_k      = arguments.get("top_k", DEFAULT_TOP_K)
        filters    = arguments.get("filters", {})
        pii_token  = arguments.get("pii_access_token")
        purpose    = arguments.get("purpose", "")

        vector = await embed_text(query)

        must_conditions = [
            FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()
        ]
        qdrant_filter = Filter(must=must_conditions) if must_conditions else None
        oversample_k  = top_k * OVERSAMPLE_FACTOR if RERANKER_ENABLED else top_k

        with _otel_span("qdrant.search"):
            results = await qdrant.query_points(
                collection_name=collection, query=vector,
                query_filter=qdrant_filter, limit=oversample_k, with_payload=True,
            )

        filtered = []
        for hit in results.points:
            classification = hit.payload.get("classification", "internal")
            policy = await check_opa_policy(agent_id, agent_role,
                                            f"{collection}/{hit.id}", classification)
            if policy["allowed"]:
                filtered.append({
                    "id": str(hit.id), "score": round(hit.score, 4),
                    "content": hit.payload.get("text", hit.payload.get("content", "")),
                    "metadata": {k: v for k, v in hit.payload.items()
                                 if k not in ("content", "text")},
                })

        reranked = await rerank_results(query, filtered, top_n=top_k)
        mcp_search_results_count.labels(collection=collection).observe(len(reranked))

        pool = await get_pg_pool()
        await check_feedback_warning(query, pool)

        # Vault resolution: if token provided, try to resolve originals
        if pii_token and purpose:
            token_result = validate_pii_access_token(pii_token)
            if token_result["valid"]:
                import hashlib as _hl
                token_hash = _hl.sha256(
                    json.dumps(pii_token, sort_keys=True).encode()
                ).hexdigest()[:16]

                for item in reranked:
                    vault_ref = item.get("metadata", {}).get("vault_ref")
                    if not vault_ref:
                        continue
                    doc_id = item.get("metadata", {}).get("document_id")
                    if not doc_id:
                        # Try to find doc_id from vault_ref
                        doc_row = await pool.fetchrow(
                            "SELECT document_id FROM pii_vault.original_content WHERE id = $1",
                            vault_ref,
                        )
                        doc_id = str(doc_row["document_id"]) if doc_row else None
                    if not doc_id:
                        continue

                    classification = item.get("metadata", {}).get("classification", "internal")
                    data_category = item.get("metadata", {}).get("data_category", "")

                    vault_policy = await check_opa_vault_access(
                        agent_role, purpose, classification,
                        data_category, True, False,
                    )
                    if vault_policy["allowed"]:
                        vault_data = await vault_lookup(doc_id, [item.get("metadata", {}).get("chunk_index", 0)])
                        if vault_data:
                            original = vault_data[0]
                            redacted_text = redact_fields(
                                original["original_text"],
                                original["pii_entities"],
                                set(vault_policy["fields_to_redact"]),
                            )
                            item["original_content"] = redacted_text
                            item["vault_access"] = True

                            await log_vault_access(
                                agent_id, doc_id,
                                item.get("metadata", {}).get("chunk_index"),
                                purpose, token_hash,
                            )

        await log_access(agent_id, agent_role, "search", collection, "search", "allow", {
            "query": arguments["query"], "qdrant_results": len(results.points),
            "after_policy": len(filtered), "after_rerank": len(reranked),
            "vault_access_requested": pii_token is not None,
        })
        return [TextContent(type="text",
            text=json.dumps({"results": reranked, "total": len(reranked)}, ensure_ascii=False, indent=2))]

    # ── query_data ───────────────────────────────────────────
    elif name == "query_data":
        dataset    = arguments["dataset"]
        conditions = arguments.get("conditions", {})
        limit      = arguments.get("limit", 50)
        pool = await get_pg_pool()

        ds = await pool.fetchrow(
            "SELECT id, classification FROM datasets WHERE name = $1 OR id::text = $1", dataset
        )
        if not ds:
            return [TextContent(type="text",
                text=json.dumps({"error": f"Dataset '{dataset}' nicht gefunden"}))]

        policy = await check_opa_policy(agent_id, agent_role,
                                        f"dataset/{ds['id']}", ds["classification"])
        if not policy["allowed"]:
            await log_access(agent_id, agent_role, "dataset", str(ds["id"]), "query", "deny")
            return [TextContent(type="text",
                text=json.dumps({"error": "Zugriff verweigert", "classification": ds["classification"]}))]

        where_clauses = ["dataset_id = $1"]
        params: list[Any] = [ds["id"]]
        idx = 2
        for key, value in conditions.items():
            if not validate_identifier(key):
                return [TextContent(type="text",
                    text=json.dumps({"error": f"Ungültiger Condition-Key: {key!r}"}))]
            where_clauses.append(f"data->>'{key}' = ${idx}")
            params.append(str(value))
            idx += 1

        q = f"SELECT data FROM dataset_rows WHERE {' AND '.join(where_clauses)} LIMIT ${idx}"
        params.append(int(limit))
        rows = await pool.fetch(q, *params)

        await log_access(agent_id, agent_role, "dataset", str(ds["id"]), "query", "allow")
        return [TextContent(type="text",
            text=json.dumps({"rows": [json.loads(r["data"]) for r in rows], "count": len(rows)},
                            ensure_ascii=False, indent=2))]

    # ── get_rules ────────────────────────────────────────────
    elif name == "get_rules":
        category = arguments["category"]
        context  = arguments.get("context", {})
        try:
            resp = await http.post(
                f"{OPA_URL}/v1/data/kb/rules/{category}",
                json={"input": {"context": context, "agent_role": agent_role}}
            )
            resp.raise_for_status()
            rules = resp.json().get("result", {})
        except Exception as e:
            rules = {"error": f"Regeln konnten nicht abgerufen werden: {str(e)}"}

        await log_access(agent_id, agent_role, "rule", category, "get_rules", "allow")
        return [TextContent(type="text",
            text=json.dumps({"category": category, "rules": rules}, ensure_ascii=False, indent=2))]

    # ── check_policy ─────────────────────────────────────────
    elif name == "check_policy":
        result = await check_opa_policy(
            agent_id, agent_role, arguments["resource"],
            arguments["classification"], arguments.get("action", "read")
        )
        await log_access(agent_id, agent_role, "policy", arguments["resource"],
                         "check_policy", "allow" if result["allowed"] else "deny")
        return [TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── ingest_data ──────────────────────────────────────────
    elif name == "ingest_data":
        try:
            resp = await http.post(f"{INGESTION_URL}/ingest", json={
                "source": arguments["source"],
                "source_type": arguments["source_type"],
                "project": arguments.get("project"),
                "classification": arguments.get("classification", "internal"),
                "metadata": arguments.get("metadata", {}),
            })
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            result = {"error": f"Ingestion fehlgeschlagen: {str(e)}"}

        await log_access(agent_id, agent_role, "ingestion", arguments["source"],
                         "ingest", "allow", {"source_type": arguments["source_type"]})
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── list_datasets ────────────────────────────────────────
    elif name == "list_datasets":
        pool = await get_pg_pool()
        q = "SELECT id, name, source_type, project, classification, created_at FROM datasets WHERE 1=1"
        params: list[Any] = []
        idx = 1

        if arguments.get("project"):
            q += f" AND project = ${idx}"
            params.append(arguments["project"])
            idx += 1
        if arguments.get("source_type"):
            q += f" AND source_type = ${idx}"
            params.append(arguments["source_type"])
            idx += 1

        q += " ORDER BY created_at DESC LIMIT 100"
        rows = await pool.fetch(q, *params)

        datasets = []
        for r in rows:
            policy = await check_opa_policy(agent_id, agent_role,
                                            f"dataset/{r['id']}", r["classification"])
            if policy["allowed"]:
                datasets.append({
                    "id": str(r["id"]), "name": r["name"],
                    "project": r["project"], "classification": r["classification"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                })

        return [TextContent(type="text",
            text=json.dumps({"datasets": datasets, "count": len(datasets)}, ensure_ascii=False, indent=2))]

    # ── get_code_context ─────────────────────────────────────
    elif name == "get_code_context":
        query  = arguments["query"]
        top_k  = arguments.get("top_k", 5)
        vector = await embed_text(query)

        filters_list = []
        if arguments.get("repo"):
            filters_list.append(FieldCondition(key="repo", match=MatchValue(value=arguments["repo"])))
        if arguments.get("language"):
            filters_list.append(FieldCondition(key="language", match=MatchValue(value=arguments["language"])))

        qdrant_filter = Filter(must=filters_list) if filters_list else None
        oversample_k  = top_k * OVERSAMPLE_FACTOR if RERANKER_ENABLED else top_k

        results = await qdrant.query_points(
            collection_name="knowledge_code", query=vector,
            query_filter=qdrant_filter, limit=oversample_k, with_payload=True,
        )

        code_results = []
        for hit in results.points:
            classification = hit.payload.get("classification", "internal")
            policy = await check_opa_policy(agent_id, agent_role, f"code/{hit.id}", classification)
            if policy["allowed"]:
                code_results.append({
                    "id": str(hit.id), "score": round(hit.score, 4),
                    "content": hit.payload.get("content", ""),
                    "metadata": {"repo": hit.payload.get("repo"),
                                 "path": hit.payload.get("path"),
                                 "language": hit.payload.get("language")},
                })

        reranked = await rerank_results(query, code_results, top_n=top_k)
        await log_access(agent_id, agent_role, "code", "knowledge_code", "search", "allow", {
            "query": query, "qdrant_results": len(results.points),
            "after_policy": len(code_results), "after_rerank": len(reranked),
        })
        return [TextContent(type="text",
            text=json.dumps({"results": reranked, "total": len(reranked)}, ensure_ascii=False, indent=2))]

    # ── get_classification ───────────────────────────────────
    elif name == "get_classification":
        resource_id   = arguments["resource_id"]
        resource_type = arguments["resource_type"]
        pool = await get_pg_pool()

        if resource_type == "dataset":
            row = await pool.fetchrow(
                "SELECT classification FROM datasets WHERE id::text = $1 OR name = $1", resource_id
            )
        elif resource_type == "document":
            row = await pool.fetchrow(
                "SELECT classification FROM documents_meta WHERE id::text = $1", resource_id
            )
        else:
            row = None

        if row:
            return [TextContent(type="text", text=json.dumps(
                {"resource_id": resource_id, "type": resource_type, "classification": row["classification"]}
            ))]
        return [TextContent(type="text", text=json.dumps({"error": "Ressource nicht gefunden"}))]

    # ── graph_query ──────────────────────────────────────────
    elif name == "graph_query":
        action = arguments["action"]
        pool   = await get_pg_pool()
        try:
            if action == "find_node":
                results = await graph.find_node(pool, arguments.get("label", "Project"),
                                                arguments.get("properties", {}))
                data = {"nodes": results, "count": len(results)}
            elif action == "find_relationships":
                results = await graph.find_relationships(
                    pool,
                    from_label=arguments.get("from_label") or arguments.get("label"),
                    from_id=arguments.get("node_id"),
                    rel_type=arguments.get("rel_type"),
                    to_label=arguments.get("to_label"),
                    to_id=arguments.get("to_id"),
                    depth=arguments.get("max_depth", 1),
                )
                data = {"relationships": results, "count": len(results)}
            elif action == "get_neighbors":
                results = await graph.get_neighbors(
                    pool, arguments.get("label", "Project"), arguments["node_id"],
                    direction=arguments.get("direction", "both"),
                    max_depth=arguments.get("max_depth", 1),
                )
                data = {"neighbors": results, "count": len(results)}
            elif action == "find_path":
                results = await graph.find_path(
                    pool,
                    from_label=arguments.get("label", "Project"), from_id=arguments["node_id"],
                    to_label=arguments["to_label"], to_id=arguments["to_id"],
                    max_depth=arguments.get("max_depth", 5),
                )
                data = {"path": results}
            elif action == "get_subgraph":
                data = await graph.get_subgraph(
                    pool, arguments.get("label", "Project"), arguments["node_id"],
                    max_depth=arguments.get("max_depth", 2),
                )
            else:
                data = {"error": f"Unbekannte Graph-Action: {action}"}
        except Exception as e:
            log.error(f"Graph-Query fehlgeschlagen: {e}")
            data = {"error": str(e)}

        await log_access(agent_id, agent_role, "graph", action, "graph_query", "allow")
        return [TextContent(type="text",
            text=json.dumps(data, ensure_ascii=False, indent=2, default=str))]

    # ── graph_mutate ─────────────────────────────────────────
    elif name == "graph_mutate":
        if agent_role not in ("developer", "admin"):
            await log_access(agent_id, agent_role, "graph", arguments["action"], "graph_mutate", "deny")
            return [TextContent(type="text",
                text=json.dumps({"error": "Graph-Mutationen erfordern developer- oder admin-Rolle"}))]

        action = arguments["action"]
        pool   = await get_pg_pool()
        try:
            if action == "create_node":
                result = await graph.create_node(pool, arguments["label"],
                                                  arguments.get("properties", {}))
                data = {"created": result}
            elif action == "delete_node":
                await graph.delete_node(pool, arguments["label"], arguments["node_id"])
                data = {"deleted": True, "label": arguments["label"], "id": arguments["node_id"]}
            elif action == "create_relationship":
                result = await graph.create_relationship(
                    pool,
                    from_label=arguments["from_label"], from_id=arguments["from_id"],
                    to_label=arguments["to_label"], to_id=arguments["to_id"],
                    rel_type=arguments["rel_type"],
                    properties=arguments.get("rel_properties"),
                )
                data = {"created": result}
            else:
                data = {"error": f"Unbekannte Graph-Mutation: {action}"}
        except Exception as e:
            log.error(f"Graph-Mutation fehlgeschlagen: {e}")
            data = {"error": str(e)}

        await log_access(agent_id, agent_role, "graph", action, "graph_mutate", "allow")
        return [TextContent(type="text",
            text=json.dumps(data, ensure_ascii=False, indent=2, default=str))]

    # ── submit_feedback (Baustein 3) ─────────────────────────
    elif name == "submit_feedback":
        query          = arguments["query"]
        result_ids     = arguments["result_ids"]
        rating         = arguments["rating"]
        relevant_ids   = arguments.get("relevant_ids")
        irrelevant_ids = arguments.get("irrelevant_ids")
        comment        = arguments.get("comment")
        collection     = arguments.get("collection")
        rerank_scores  = arguments.get("rerank_scores")

        pool = await get_pg_pool()
        row = await pool.fetchrow("""
            INSERT INTO search_feedback
                (query, result_ids, rating, agent_id, comment,
                 relevant_ids, irrelevant_ids, collection, rerank_scores)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
        """, query, result_ids, rating, agent_id, comment,
             relevant_ids, irrelevant_ids, collection,
             json.dumps(rerank_scores) if rerank_scores else None)

        # Gauge aktualisieren (letzte 24h)
        avg_row = await pool.fetchrow("""
            SELECT AVG(rating) FROM search_feedback
            WHERE created_at > now() - interval '24 hours'
        """)
        if avg_row and avg_row["avg"] is not None:
            mcp_feedback_avg_rating.set(float(avg_row["avg"]))

        return [TextContent(type="text",
            text=json.dumps({"feedback_id": row["id"], "stored": True}, indent=2))]

    # ── get_eval_stats (Baustein 3) ──────────────────────────
    elif name == "get_eval_stats":
        days = arguments.get("days", 30)
        pool = await get_pg_pool()

        # Gesamt-Statistik im Zeitraum
        stats = await pool.fetchrow("""
            SELECT
                COUNT(*)                          AS total_feedback,
                ROUND(AVG(rating)::numeric, 2)    AS avg_rating,
                ROUND(100.0 * SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1)
                                                  AS satisfaction_pct
            FROM search_feedback
            WHERE created_at > now() - ($1 || ' days')::interval
        """, str(days))

        # Top-10 schlecht bewertete Queries
        worst = await pool.fetch("""
            SELECT query, ROUND(AVG(rating)::numeric, 2) AS avg_rating, COUNT(*) AS feedback_count
            FROM search_feedback
            WHERE created_at > now() - ($1 || ' days')::interval
            GROUP BY query
            HAVING COUNT(*) >= 2
            ORDER BY avg_rating ASC
            LIMIT 10
        """, str(days))

        # Trend: Vergleich aktuelle vs. vorherige Periode
        trend_current = await pool.fetchrow("""
            SELECT ROUND(AVG(rating)::numeric, 2) AS avg_rating
            FROM search_feedback
            WHERE created_at > now() - ($1 || ' days')::interval
        """, str(days))
        trend_previous = await pool.fetchrow("""
            SELECT ROUND(AVG(rating)::numeric, 2) AS avg_rating
            FROM search_feedback
            WHERE created_at BETWEEN now() - ($1 || ' days')::interval * 2
                          AND now() - ($1 || ' days')::interval
        """, str(days))

        result = {
            "period_days": days,
            "total_feedback": stats["total_feedback"],
            "avg_rating": float(stats["avg_rating"]) if stats["avg_rating"] else None,
            "satisfaction_pct": float(stats["satisfaction_pct"]) if stats["satisfaction_pct"] else None,
            "worst_queries": [
                {"query": r["query"], "avg_rating": float(r["avg_rating"]),
                 "feedback_count": r["feedback_count"]}
                for r in worst
            ],
            "trend": {
                "current_period_avg":  float(trend_current["avg_rating"]) if trend_current["avg_rating"] else None,
                "previous_period_avg": float(trend_previous["avg_rating"]) if trend_previous["avg_rating"] else None,
            },
        }
        return [TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── create_snapshot (Baustein 4) ─────────────────────────
    elif name == "create_snapshot":
        if agent_role != "admin":
            return [TextContent(type="text",
                text=json.dumps({"error": "Snapshots erstellen erfordert admin-Rolle"}))]

        snapshot_name = arguments["name"]
        description   = arguments.get("description", "")

        try:
            resp = await http.post(f"{INGESTION_URL}/snapshots/create", json={
                "name": snapshot_name, "description": description,
                "created_by": agent_id,
            })
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            result = {"error": f"Snapshot-Erstellung fehlgeschlagen: {str(e)}"}

        await log_access(agent_id, agent_role, "snapshot", snapshot_name,
                         "create_snapshot", "allow")
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── list_snapshots (Baustein 4) ──────────────────────────
    elif name == "list_snapshots":
        limit = arguments.get("limit", 10)
        pool  = await get_pg_pool()

        rows = await pool.fetch("""
            SELECT id, snapshot_name, created_at, created_by, description,
                   components, status, size_bytes
            FROM knowledge_snapshots
            ORDER BY created_at DESC
            LIMIT $1
        """, limit)

        snapshots = [
            {
                "id": r["id"],
                "name": r["snapshot_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "created_by": r["created_by"],
                "description": r["description"],
                "status": r["status"],
                "size_bytes": r["size_bytes"],
                "components": r["components"],
            }
            for r in rows
        ]
        return [TextContent(type="text",
            text=json.dumps({"snapshots": snapshots, "count": len(snapshots)},
                            ensure_ascii=False, indent=2, default=str))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unbekanntes Tool: {name}"}))]


# ── Startup ──────────────────────────────────────────────────
if __name__ == "__main__":
    prom_start_http_server(METRICS_PORT)
    log.info(f"Prometheus /metrics auf Port {METRICS_PORT}")

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
    )

    # Must be a class instance (not async def) so Starlette treats it
    # as a raw ASGI app instead of wrapping it with request_response().
    class MCPTransport:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await session_manager.handle_request(scope, receive, send)

    app = Starlette(
        routes=[Route(MCP_PATH, endpoint=MCPTransport())],
        lifespan=lambda app: session_manager.run(),
    )

    # ── Auth-Middleware (inside-out: last applied = outermost) ──
    verifier = ApiKeyVerifier()
    # AuthContextMiddleware: stores authenticated user in contextvars
    app = AuthContextMiddleware(app)
    if AUTH_REQUIRED:
        # RequireAuthMiddleware: rejects unauthenticated requests with 401
        app = RequireAuthMiddleware(app, required_scopes=[])
    # AuthenticationMiddleware: extracts Bearer token, calls verifier
    app = AuthenticationMiddleware(app, backend=BearerAuthBackend(verifier))

    mode = "enforced" if AUTH_REQUIRED else "optional"
    log.info("MCP Streamable HTTP auf %s:%s%s (auth: %s)", MCP_HOST, MCP_PORT, MCP_PATH, mode)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
