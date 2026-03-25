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

import asyncio
import os
import json
import logging
import time
from contextlib import asynccontextmanager
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
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route
from starlette.types import Scope, Receive, Send
from starlette.middleware.authentication import AuthenticationMiddleware
from prometheus_client import (
    Counter, Histogram, Gauge,
    start_http_server as prom_start_http_server,
)
import uvicorn
import hashlib
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.llm_provider import EmbeddingProvider, CompletionProvider
from shared.config import read_secret, build_postgres_url, PG_POOL_MIN, PG_POOL_MAX
from shared.telemetry import (
    init_telemetry, setup_auto_instrumentation, trace_operation,
    request_telemetry_context, get_current_telemetry,
    MetricsAggregator, TELEMETRY_IN_RESPONSE,
)

import graph_service as graph
from graph_service import validate_identifier

# ── Konfiguration ────────────────────────────────────────────

QDRANT_URL    = os.getenv("QDRANT_URL",    "http://localhost:6333")
POSTGRES_URL  = build_postgres_url()
OPA_URL       = os.getenv("OPA_URL",       "http://localhost:8181")
FORGEJO_URL   = os.getenv("FORGEJO_URL",   "http://forgejo.local:3000")
FORGEJO_TOKEN = read_secret("FORGEJO_TOKEN")
RERANKER_URL  = os.getenv("RERANKER_URL",  "http://reranker:8082")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() == "true"
INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")

# ── Backward-compat fallback ──
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# ── Embedding provider ──
EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL        = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY      = os.getenv("EMBEDDING_API_KEY", "")

# ── LLM / Summarization provider ──
LLM_PROVIDER_URL       = os.getenv("LLM_PROVIDER_URL", _OLLAMA_URL)
LLM_MODEL              = os.getenv("LLM_MODEL", "qwen2.5:3b")
LLM_API_KEY            = os.getenv("LLM_API_KEY", "")
SUMMARIZATION_ENABLED  = os.getenv("SUMMARIZATION_ENABLED", "true").lower() == "true"

embedding_provider = EmbeddingProvider(base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY)
llm_provider       = CompletionProvider(base_url=LLM_PROVIDER_URL, api_key=LLM_API_KEY)

from shared.embedding_cache import EmbeddingCache
embedding_cache = EmbeddingCache()

MCP_HOST       = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT       = int(os.getenv("MCP_PORT", "8080"))
MCP_PATH       = os.getenv("MCP_PATH", "/mcp")
METRICS_PORT   = int(os.getenv("METRICS_PORT", "9091"))
AUTH_REQUIRED  = os.getenv("AUTH_REQUIRED", "true").lower() == "true"

RATE_LIMIT_ENABLED    = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_ANALYST    = int(os.getenv("RATE_LIMIT_ANALYST", "60"))
RATE_LIMIT_DEVELOPER  = int(os.getenv("RATE_LIMIT_DEVELOPER", "120"))
RATE_LIMIT_ADMIN      = int(os.getenv("RATE_LIMIT_ADMIN", "300"))
RATE_LIMITS_BY_ROLE   = {
    "analyst": RATE_LIMIT_ANALYST,
    "developer": RATE_LIMIT_DEVELOPER,
    "admin": RATE_LIMIT_ADMIN,
}
DEFAULT_TOP_K      = 10
OVERSAMPLE_FACTOR  = 5

# Feedback-Loop: Warnung wenn avg_rating unter diesem Schwellwert mit mind. N Feedbacks
FEEDBACK_WARN_THRESHOLD = 2.5
FEEDBACK_WARN_MIN_COUNT = 3

# ── OPA Cache ────────────────────────────────────────────────
from cachetools import TTLCache as _TTLCache

OPA_CACHE_TTL     = int(os.getenv("OPA_CACHE_TTL", "60"))
OPA_CACHE_ENABLED = os.getenv("OPA_CACHE_ENABLED", "true").lower() == "true"
_opa_cache: _TTLCache[str, bool] = _TTLCache(maxsize=64, ttl=OPA_CACHE_TTL)
_opa_cache_lock = __import__("threading").Lock()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pb-mcp")

# ── Prometheus Metriken ──────────────────────────────────────
mcp_requests_total = Counter(
    "pb_mcp_requests_total",
    "MCP-Requests pro Tool und Status",
    ["tool", "status"],
)
mcp_request_duration = Histogram(
    "pb_mcp_request_duration_seconds",
    "Latenz pro MCP-Tool",
    ["tool"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)
mcp_policy_decisions_total = Counter(
    "pb_mcp_policy_decisions_total",
    "OPA Policy-Entscheidungen",
    ["result"],
)
mcp_search_results_count = Histogram(
    "pb_mcp_search_results_count",
    "Anzahl Suchergebnisse nach Reranking",
    ["collection"],
    buckets=[0, 1, 3, 5, 10, 20, 50],
)
mcp_rerank_fallback_total = Counter(
    "pb_mcp_rerank_fallback_total",
    "Anzahl Reranker-Fallbacks (nicht erreichbar)",
)
mcp_feedback_avg_rating = Gauge(
    "pb_feedback_avg_rating",
    "Aktueller Durchschnitt des Feedback-Ratings (letzte 24h)",
)
mcp_rate_limit_rejected = Counter(
    "pb_rate_limit_rejected_total",
    "Requests rejected by rate limiter",
    ["agent_role"],
)

# ── OpenTelemetry Setup ──────────────────────────────────────
tracer = init_telemetry("pb-mcp-server")
_metrics_agg = MetricsAggregator("mcp-server")


def _parse_prom_labels(key: str) -> dict[str, str]:
    """Parse 'metric{k1=v1,k2=v2}' into dict."""
    if "{" not in key:
        return {}
    label_str = key.split("{", 1)[1].rstrip("}")
    labels = {}
    for part in label_str.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            labels[k] = v
    return labels


def _opa_cache_hit_ratio() -> float:
    """Calculate OPA cache hit ratio from cache stats."""
    with _opa_cache_lock:
        total = getattr(_opa_cache, '_hits', 0) + getattr(_opa_cache, '_misses', 0)
        if total == 0:
            return 0.0
        return round(getattr(_opa_cache, '_hits', 0) / total, 3)


# ── Rate Limiting ────────────────────────────────────────────

class TokenBucket:
    """In-memory token bucket for rate limiting."""

    def __init__(self, capacity: float, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate  # tokens per second
        self.last_refill = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0.0
        self._lock = asyncio.Lock()
        self.last_used = self.last_refill

    async def consume(self) -> tuple[bool, float]:
        """Try to consume a token. Returns (allowed, retry_after_seconds)."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            self.last_used = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0
            else:
                retry_after = (1.0 - self.tokens) / self.refill_rate
                return False, retry_after


_rate_limit_buckets: dict[str, TokenBucket] = {}
_rate_limit_cleanup_counter = 0


def _get_bucket(agent_id: str, role: str) -> TokenBucket:
    """Get or create a token bucket for an agent."""
    global _rate_limit_cleanup_counter
    if agent_id not in _rate_limit_buckets:
        rpm = RATE_LIMITS_BY_ROLE.get(role, RATE_LIMIT_ANALYST)
        _rate_limit_buckets[agent_id] = TokenBucket(
            capacity=float(rpm),
            refill_rate=rpm / 60.0,
        )
    # Periodic cleanup of stale buckets (every 100 requests)
    _rate_limit_cleanup_counter += 1
    if _rate_limit_cleanup_counter >= 100:
        _rate_limit_cleanup_counter = 0
        now = asyncio.get_event_loop().time()
        stale = [k for k, v in _rate_limit_buckets.items()
                 if now - v.last_used > 600]
        for k in stale:
            del _rate_limit_buckets[k]
    return _rate_limit_buckets[agent_id]


class RateLimitMiddleware:
    """Starlette middleware for per-agent rate limiting."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not RATE_LIMIT_ENABLED:
            return await self.app(scope, receive, send)

        # Skip rate limiting for health/metrics endpoints
        path = scope.get("path", "")
        if path in ("/health", "/metrics"):
            return await self.app(scope, receive, send)

        try:
            # Extract agent info from auth state (set by AuthContextMiddleware)
            user = scope.get("user")
            if user and hasattr(user, "identity"):
                agent_id = user.identity
                role = user.scopes[0] if user.scopes else "analyst"
                bucket = _get_bucket(agent_id, role)
                allowed, retry_after = await bucket.consume()

                if not allowed:
                    mcp_rate_limit_rejected.labels(agent_role=role).inc()
                    response_body = json.dumps({
                        "error": "Rate limit exceeded",
                        "retry_after": round(retry_after, 1),
                    }).encode()
                    await send({
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"Retry-After", str(int(retry_after) + 1).encode()],
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": response_body,
                    })
                    return
        except Exception as e:
            # Fail open — rate limiter error should not block requests
            log.warning(f"Rate limiter Fehler, Request wird durchgelassen: {e}")

        return await self.app(scope, receive, send)


# ── Clients ──────────────────────────────────────────────────
qdrant  = AsyncQdrantClient(url=QDRANT_URL, timeout=30)
http    = httpx.AsyncClient(timeout=30.0)
pg_pool: asyncpg.Pool | None = None


async def get_pg_pool() -> asyncpg.Pool:
    if pg_pool is None:
        raise RuntimeError("PG pool not initialized — server not started via lifespan")
    return pg_pool


# ── Qdrant Filter Builder ───────────────────────────────────

def _build_qdrant_filter(filters: dict | None, layer: str | None = None) -> Filter | None:
    """Build Qdrant Filter from user filters + optional layer constraint.

    Args:
        filters: Key-value pairs for exact-match payload filtering.
        layer: Optional context layer (L0, L1, L2) to restrict results.

    Returns:
        A Qdrant Filter with must-conditions, or None if no conditions.
    """
    must_conditions = []
    if filters:
        must_conditions.extend(
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in filters.items()
        )
    if layer:
        must_conditions.append(
            FieldCondition(key="layer", match=MatchValue(value=layer))
        )
    return Filter(must=must_conditions) if must_conditions else None


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
        # Update last_used_at (throttled: only if >5 min old, fire-and-forget)
        try:
            await pool.execute(
                "UPDATE api_keys SET last_used_at = now() "
                "WHERE key_hash = $1 AND (last_used_at IS NULL "
                "OR last_used_at < now() - interval '5 minutes')",
                key_hash,
            )
        except Exception:
            pass  # Non-critical, don't fail auth over this
        return AccessToken(
            token=token,
            client_id=row["agent_id"],
            scopes=[row["agent_role"]],
        )


# ── Hilfsfunktionen ──
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
    before_sleep=lambda rs: log.warning(f"Embed retry #{rs.attempt_number} nach Fehler: {rs.outcome.exception()}"),
)
async def embed_text(text: str) -> list[float]:
    with trace_operation(tracer, "embedding", "mcp-server",
                         model=EMBEDDING_MODEL, text_length=len(text)):
        cached = embedding_cache.get(text, EMBEDDING_MODEL)
        if cached is not None:
            return cached
        vector = await embedding_provider.embed(http, text, EMBEDDING_MODEL)
        embedding_cache.set(text, EMBEDDING_MODEL, vector)
        return vector


async def summarize_text(
    chunks: list[str],
    query: str,
    detail: str = "standard",
) -> str | None:
    """Summarize chunks via LLM provider. Returns None on failure (graceful degradation)."""
    if not chunks:
        return None

    detail_instructions = {
        "brief": "Provide a very concise summary in 1-2 sentences.",
        "standard": "Provide a clear summary covering the key points.",
        "detailed": "Provide a comprehensive summary preserving important details.",
    }

    system_prompt = (
        "You are a context summarization engine. Summarize the provided text chunks "
        "to answer the user's query. Only use information from the provided chunks. "
        "Do not add information that is not in the chunks. "
        f"{detail_instructions.get(detail, detail_instructions['standard'])}"
    )

    combined = "\n\n---\n\n".join(f"Chunk {i+1}:\n{c}" for i, c in enumerate(chunks))
    user_prompt = f"Query: {query}\n\nText chunks to summarize:\n\n{combined}"

    with trace_operation(tracer, "summarization", "mcp-server",
                         model=LLM_MODEL):
        try:
            return await llm_provider.generate(
                http,
                model=LLM_MODEL,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as e:
            log.warning(f"Summarization failed, returning raw chunks: {e}")
            return None


async def check_opa_summarization_policy(
    agent_role: str,
    classification: str,
) -> dict:
    """Check OPA summarization policy. Returns {allowed, required, detail}."""
    input_data = {
        "agent_role": agent_role,
        "classification": classification,
    }
    try:
        allowed_resp = await http.post(
            f"{OPA_URL}/v1/data/pb/summarization/summarize_allowed",
            json={"input": input_data},
        )
        allowed_resp.raise_for_status()
        allowed = allowed_resp.json().get("result", False)

        required_resp = await http.post(
            f"{OPA_URL}/v1/data/pb/summarization/summarize_required",
            json={"input": input_data},
        )
        required_resp.raise_for_status()
        required = required_resp.json().get("result", False)

        detail_resp = await http.post(
            f"{OPA_URL}/v1/data/pb/summarization/summarize_detail",
            json={"input": input_data},
        )
        detail_resp.raise_for_status()
        detail = detail_resp.json().get("result", "standard")
    except Exception as e:
        log.warning(f"OPA summarization policy check failed: {e}")
        allowed = False
        required = False
        detail = "standard"

    return {"allowed": allowed, "required": required, "detail": detail}


async def rerank_results(query: str, documents: list[dict], top_n: int) -> list[dict]:
    if not RERANKER_ENABLED or not documents:
        return documents[:top_n]

    with trace_operation(tracer, "reranking", "mcp-server",
                         input_count=len(documents), top_n=top_n):
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


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
    before_sleep=lambda rs: log.warning(f"OPA retry #{rs.attempt_number} nach Fehler: {rs.outcome.exception()}"),
)
async def check_opa_policy(agent_id: str, agent_role: str,
                           resource: str, classification: str,
                           action: str = "read") -> dict:
    with trace_operation(tracer, "opa_policy", "mcp-server",
                         role=agent_role, classification=classification):
        # Cache lookup — key is (role, classification, action) since
        # pb.access.allow only depends on these three fields.
        cache_key = f"{agent_role}:{classification}:{action}"
        if OPA_CACHE_ENABLED:
            with _opa_cache_lock:
                cached = _opa_cache.get(cache_key)
            if cached is not None:
                mcp_policy_decisions_total.labels(result="allow" if cached else "deny").inc()
                return {"allowed": cached, "input": {
                    "agent_id": agent_id, "agent_role": agent_role,
                    "resource": resource, "classification": classification, "action": action,
                }}

        input_data = {
            "agent_id": agent_id, "agent_role": agent_role,
            "resource": resource, "classification": classification, "action": action,
        }
        try:
            resp = await http.post(
                f"{OPA_URL}/v1/data/pb/access/allow", json={"input": input_data}
            )
            resp.raise_for_status()
            allowed = resp.json().get("result", False)
        except (httpx.ConnectError, httpx.TimeoutException):
            raise  # Let tenacity retry these
        except Exception as e:
            log.warning(f"OPA check failed, defaulting to deny: {e}")
            allowed = False

        # Store in cache
        if OPA_CACHE_ENABLED:
            with _opa_cache_lock:
                _opa_cache[cache_key] = allowed

        mcp_policy_decisions_total.labels(result="allow" if allowed else "deny").inc()
        return {"allowed": allowed, "input": input_data}



async def filter_by_policy(
    hits: list,
    agent_id: str,
    agent_role: str,
    resource_prefix: str,
) -> list:
    """Check OPA policies for all hits in parallel, return allowed ones."""
    if not hits:
        return []

    async def _check(hit):
        classification = hit.payload.get("classification", "internal")
        policy = await check_opa_policy(
            agent_id, agent_role,
            f"{resource_prefix}/{hit.id}", classification,
        )
        if policy["allowed"]:
            return hit
        return None

    results = await asyncio.gather(*[_check(h) for h in hits])
    return [h for h in results if h is not None]


async def log_access(agent_id: str, agent_role: str,
                     resource_type: str, resource_id: str,
                     action: str, policy_result: str,
                     context: dict | None = None):
    contains_pii = False

    if context and "query" in context:
        try:
            scan_resp = None
            for attempt in range(2):  # 1 initial + 1 retry
                try:
                    scan_resp = await http.post(f"{INGESTION_URL}/scan", json={
                        "text": context["query"],
                    })
                    scan_resp.raise_for_status()
                    break
                except (httpx.ConnectError, httpx.TimeoutException):
                    if attempt == 0:
                        log.warning("PII scan retry nach Verbindungsfehler...")
                        await asyncio.sleep(1)
                    else:
                        raise
            scan_data = scan_resp.json()

            contains_pii = scan_data["contains_pii"]
            context["query"] = scan_data["masked_text"]
            if contains_pii:
                context["query_contains_pii"] = True
                context["pii_entity_types"] = scan_data["entity_types"]
        except Exception as e:
            log.warning(f"PII scan für Audit-Log fehlgeschlagen, speichere ohne Scan: {e}")
            # Continue without PII scan — better to log unscanned than to fail

    pool = await get_pg_pool()
    await pool.execute("""
        INSERT INTO agent_access_log
            (agent_id, agent_role, resource_type, resource_id,
             action, policy_result, request_context, contains_pii)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    """, agent_id, agent_role, resource_type, resource_id,
       action, policy_result, json.dumps(context or {}), contains_pii)


# ── Vault Access ────────────────────────────────────────────

VAULT_HMAC_SECRET = read_secret("VAULT_HMAC_SECRET", "change-me-in-production")


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
            f"{OPA_URL}/v1/data/pb/privacy/vault_access_allowed",
            json={"input": input_data},
        )
        resp.raise_for_status()
        allowed = resp.json().get("result", False)
        fields_resp = await http.post(
            f"{OPA_URL}/v1/data/pb/privacy/vault_fields_to_redact",
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
server = Server("pb-mcp-server")


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
                                   "enum": ["pb_general", "pb_code", "pb_rules"],
                                   "default": "pb_general"},
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
                    "summarize": {
                        "type": "boolean",
                        "default": False,
                        "description": "Request a summary of results instead of raw chunks",
                    },
                    "summary_detail": {
                        "type": "string",
                        "enum": ["brief", "standard", "detailed"],
                        "default": "standard",
                        "description": "Summary detail level (only used when summarize=true)",
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["L0", "L1", "L2"],
                        "description": "Context layer: L0=abstract (~100 tokens), L1=overview (~1-2k tokens), L2=full chunks (default). Omit for all layers.",
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
                    "source_type":    {"type": "string", "default": "text",
                                       "description": "Quelltyp (text). Weitere Typen via Adapter."},
                    "project":        {"type": "string"},
                    "classification": {"type": "string", "default": "internal"},
                    "metadata":       {"type": "object"},
                },
                "required": ["source"]
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
                    "summarize": {
                        "type": "boolean",
                        "default": False,
                        "description": "Request a summary of results instead of raw chunks",
                    },
                    "summary_detail": {
                        "type": "string",
                        "enum": ["brief", "standard", "detailed"],
                        "default": "standard",
                        "description": "Summary detail level",
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["L0", "L1", "L2"],
                        "description": "Context layer: L0=abstract (~100 tokens), L1=overview (~1-2k tokens), L2=full chunks (default). Omit for all layers.",
                    },
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
        # ── Context Layers: get_document ────────────────────────────
        Tool(
            name="get_document",
            description="Retrieve a specific document by ID at a given context layer. "
                        "Use L0 for abstract (~100 tokens), L1 for overview (~1-2k tokens), "
                        "L2 for full content chunks. Enables progressive context loading.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id":     {"type": "string",
                                   "description": "Document ID (from search results metadata.doc_id)"},
                    "layer":      {"type": "string", "enum": ["L0", "L1", "L2"], "default": "L1",
                                   "description": "Context layer to retrieve"},
                    "collection": {"type": "string", "default": "pb_general"},
                },
                "required": ["doc_id"]
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

    # Generate trace_id from OTel or fallback
    import uuid as _uuid
    trace_id = _uuid.uuid4().hex[:16]
    if tracer:
        try:
            from opentelemetry import trace as _trace
            span = _trace.get_current_span()
            ctx = span.get_span_context()
            if ctx.trace_id:
                trace_id = format(ctx.trace_id, '032x')
        except Exception:
            pass

    with request_telemetry_context(trace_id) as req_telemetry:
        with trace_operation(tracer, f"mcp.{name}", "mcp-server", tool=name):
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
        collection = arguments.get("collection", "pb_general")
        query      = arguments["query"]
        top_k      = arguments.get("top_k", DEFAULT_TOP_K)
        filters    = arguments.get("filters", {})
        pii_token  = arguments.get("pii_access_token")
        purpose    = arguments.get("purpose", "")

        layer = arguments.get("layer")
        vector = await embed_text(query)

        qdrant_filter = _build_qdrant_filter(filters, layer)
        oversample_k  = top_k * OVERSAMPLE_FACTOR if RERANKER_ENABLED else top_k

        with trace_operation(tracer, "vector_search", "mcp-server",
                             collection=collection, top_k=oversample_k):
            results = await qdrant.query_points(
                collection_name=collection, query=vector,
                query_filter=qdrant_filter, limit=oversample_k, with_payload=True,
            )

        allowed_hits = await filter_by_policy(
            results.points, agent_id, agent_role, collection,
        )
        filtered = [
            {
                "id": str(hit.id), "score": round(hit.score, 4),
                "content": hit.payload.get("text", hit.payload.get("content", "")),
                "metadata": {k: v for k, v in hit.payload.items()
                             if k not in ("content", "text")},
            }
            for hit in allowed_hits
        ]

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

        # ── Summarization (policy-controlled) ──
        summarize_requested = arguments.get("summarize", False)
        summary_detail = arguments.get("summary_detail", "standard")
        summary = None
        summary_policy = "not_requested"

        if SUMMARIZATION_ENABLED:
            result_classification = "internal"
            if reranked:
                result_classification = reranked[0].get("metadata", {}).get("classification", "internal")

            sum_policy = await check_opa_summarization_policy(agent_role, result_classification)

            if sum_policy["required"]:
                summary_detail = sum_policy["detail"]
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, summary_detail)
                summary_policy = "enforced"
                if summary:
                    for item in reranked:
                        item.pop("content", None)
            elif summarize_requested and sum_policy["allowed"]:
                detail = sum_policy["detail"] if sum_policy["detail"] != "standard" else summary_detail
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, detail)
                summary_policy = "requested"
            elif summarize_requested and not sum_policy["allowed"]:
                summary_policy = "denied"

        response_data = {"results": reranked, "total": len(reranked)}
        if summary is not None:
            response_data["summary"] = summary
        response_data["summary_policy"] = summary_policy

        # Inject per-request telemetry
        if TELEMETRY_IN_RESPONSE:
            rt = get_current_telemetry()
            if rt is not None:
                response_data["_telemetry"] = rt.to_dict()

        return [TextContent(type="text",
            text=json.dumps(response_data, ensure_ascii=False, indent=2))]

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
                f"{OPA_URL}/v1/data/pb/rules/{category}",
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
                "source_type": arguments.get("source_type", "text"),
                "project": arguments.get("project"),
                "classification": arguments.get("classification", "internal"),
                "metadata": arguments.get("metadata", {}),
            })
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            result = {"error": f"Ingestion fehlgeschlagen: {str(e)}"}

        await log_access(agent_id, agent_role, "ingestion", arguments["source"],
                         "ingest", "allow", {"source_type": arguments.get("source_type", "text")})
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

        async def _check_dataset(r):
            policy = await check_opa_policy(
                agent_id, agent_role,
                f"dataset/{r['id']}", r["classification"],
            )
            if policy["allowed"]:
                return {
                    "id": str(r["id"]), "name": r["name"],
                    "project": r["project"], "classification": r["classification"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
            return None

        checked = await asyncio.gather(*[_check_dataset(r) for r in rows])
        datasets = [d for d in checked if d is not None]

        return [TextContent(type="text",
            text=json.dumps({"datasets": datasets, "count": len(datasets)}, ensure_ascii=False, indent=2))]

    # ── get_code_context ─────────────────────────────────────
    elif name == "get_code_context":
        query  = arguments["query"]
        top_k  = arguments.get("top_k", 5)
        layer  = arguments.get("layer")
        vector = await embed_text(query)

        code_filters = {}
        if arguments.get("repo"):
            code_filters["repo"] = arguments["repo"]
        if arguments.get("language"):
            code_filters["language"] = arguments["language"]

        qdrant_filter = _build_qdrant_filter(code_filters or None, layer)
        oversample_k  = top_k * OVERSAMPLE_FACTOR if RERANKER_ENABLED else top_k

        results = await qdrant.query_points(
            collection_name="pb_code", query=vector,
            query_filter=qdrant_filter, limit=oversample_k, with_payload=True,
        )

        async def _check_code(hit):
            classification = hit.payload.get("classification", "internal")
            policy = await check_opa_policy(
                agent_id, agent_role, f"code/{hit.id}", classification,
            )
            if policy["allowed"]:
                return hit
            return None

        checked_hits = await asyncio.gather(*[_check_code(h) for h in results.points])
        allowed_hits = [h for h in checked_hits if h is not None]
        code_results = [
            {
                "id": str(hit.id), "score": round(hit.score, 4),
                "content": hit.payload.get("text", hit.payload.get("content", "")),
                "metadata": {k: v for k, v in hit.payload.items()
                             if k not in ("content", "text")},
            }
            for hit in allowed_hits
        ]

        reranked = await rerank_results(query, code_results, top_n=top_k)
        await log_access(agent_id, agent_role, "code", "pb_code", "search", "allow", {
            "query": query, "qdrant_results": len(results.points),
            "after_policy": len(code_results), "after_rerank": len(reranked),
        })

        # ── Summarization ──
        summarize_requested = arguments.get("summarize", False)
        summary_detail = arguments.get("summary_detail", "standard")
        summary = None
        summary_policy = "not_requested"

        if SUMMARIZATION_ENABLED:
            result_classification = "internal"
            if reranked:
                result_classification = reranked[0].get("metadata", {}).get("classification", "internal")

            sum_policy = await check_opa_summarization_policy(agent_role, result_classification)

            if sum_policy["required"]:
                summary_detail = sum_policy["detail"]
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, summary_detail)
                summary_policy = "enforced"
                if summary:
                    for item in reranked:
                        item.pop("content", None)
            elif summarize_requested and sum_policy["allowed"]:
                detail = sum_policy["detail"] if sum_policy["detail"] != "standard" else summary_detail
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, detail)
                summary_policy = "requested"
            elif summarize_requested and not sum_policy["allowed"]:
                summary_policy = "denied"

        response_data = {"results": reranked, "total": len(reranked)}
        if summary is not None:
            response_data["summary"] = summary
        response_data["summary_policy"] = summary_policy

        # Inject per-request telemetry
        if TELEMETRY_IN_RESPONSE:
            rt = get_current_telemetry()
            if rt is not None:
                response_data["_telemetry"] = rt.to_dict()

        return [TextContent(type="text",
            text=json.dumps(response_data, ensure_ascii=False, indent=2))]

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
                props = dict(arguments.get("properties", {}))
                if arguments.get("node_id"):
                    props.setdefault("id", arguments["node_id"])
                result = await graph.create_node(pool, arguments["label"], props)
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

    # ── get_document (Context Layers) ────────────────────────
    elif name == "get_document":
        doc_id = arguments["doc_id"]
        layer = arguments.get("layer", "L1")
        collection = arguments.get("collection", "pb_general")

        doc_filter = Filter(must=[
            FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            FieldCondition(key="layer", match=MatchValue(value=layer)),
        ])

        points, _ = await qdrant.scroll(
            collection_name=collection, scroll_filter=doc_filter,
            limit=100, with_payload=True,
        )

        # OPA access check
        if points:
            classification = points[0].payload.get("classification", "internal")
            policy = await check_opa_policy(
                agent_id, agent_role, f"document/{doc_id}", classification,
            )
            if not policy["allowed"]:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Access denied by policy", "classification": classification}))]


        # Sort L2 by chunk_index
        if layer == "L2":
            points.sort(key=lambda p: p.payload.get("chunk_index", 0))

        results = [{
            "id": str(p.id),
            "content": p.payload.get("text", ""),
            "layer": p.payload.get("layer"),
            "chunk_index": p.payload.get("chunk_index"),
            "metadata": {k: v for k, v in p.payload.items()
                         if k not in ("text", "content", "layer", "chunk_index")},
        } for p in points]

        response = {"doc_id": doc_id, "layer": layer, "results": results, "total": len(results)}
        await log_access(agent_id, agent_role, "document", doc_id, "get_document", "allow",
                         {"layer": layer, "collection": collection})
        return [TextContent(type="text", text=json.dumps(response, indent=2, default=str))]

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

    @asynccontextmanager
    async def lifespan(app):
        """Startup/shutdown lifecycle: PG pool, HTTP client, MCP session manager."""
        global pg_pool
        # ── Startup ──
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX)
        await pg_pool.fetchval("SELECT 1")
        log.info("PostgreSQL pool ready (%s)", POSTGRES_URL.split("@")[-1])

        async with session_manager.run():
            yield

        # ── Shutdown ──
        await http.aclose()
        await pg_pool.close()
        log.info("Shutdown: PG pool and HTTP client closed")

    async def health_check(request):
        return PlainTextResponse("ok")

    async def metrics_json(request):
        """Structured JSON metrics for demo-UI consumption."""
        snap = _metrics_agg.snapshot()

        response = {
            "service": "mcp-server",
            "uptime_seconds": snap["uptime_seconds"],
            "requests": {
                "total": sum(
                    v for k, v in snap["raw_metrics"].items()
                    if k.startswith("pb_mcp_requests_total")
                ),
                "by_tool": {},
                "by_status": {},
                "rate_limited": sum(
                    v for k, v in snap["raw_metrics"].items()
                    if k.startswith("pb_rate_limit_rejected_total")
                ),
            },
            "latency": {},
            "policy": {
                "decisions_total": {},
                "cache_hit_ratio": _opa_cache_hit_ratio(),
            },
            "search": {"results_avg": {}},
            "reranker": {
                "fallbacks_total": snap["raw_metrics"].get(
                    "pb_mcp_rerank_fallback_total", 0
                ),
            },
            "embedding_cache": embedding_cache.stats(),
            "feedback": {
                "avg_rating_24h": snap["raw_metrics"].get("pb_feedback_avg_rating", 0),
            },
        }

        # Aggregate by_tool and by_status from labeled counters
        for key, val in snap["raw_metrics"].items():
            if key.startswith("pb_mcp_requests_total{"):
                labels = _parse_prom_labels(key)
                tool = labels.get("tool", "unknown")
                status = labels.get("status", "unknown")
                response["requests"]["by_tool"][tool] = (
                    response["requests"]["by_tool"].get(tool, 0) + val
                )
                response["requests"]["by_status"][status] = (
                    response["requests"]["by_status"].get(status, 0) + val
                )
            elif key.startswith("pb_mcp_policy_decisions_total{"):
                labels = _parse_prom_labels(key)
                result = labels.get("result", "unknown")
                response["policy"]["decisions_total"][result] = val

        # Latency percentiles per tool
        for tool in response["requests"]["by_tool"]:
            response["latency"][tool] = _metrics_agg.histogram_percentiles(
                "pb_mcp_request_duration_seconds", {"tool": tool}
            )

        return JSONResponse(response)

    app = Starlette(
        routes=[
            Route("/health", endpoint=health_check),
            Route("/metrics/json", endpoint=metrics_json),
            Route(MCP_PATH, endpoint=MCPTransport()),
        ],
        lifespan=lifespan,
    )

    # ── Auth-Middleware (inside-out: last applied = outermost) ──
    starlette_app = app  # keep reference for lifespan bypass
    verifier = ApiKeyVerifier()
    # AuthContextMiddleware: stores authenticated user in contextvars
    app = AuthContextMiddleware(app)
    # RateLimitMiddleware: per-agent token bucket rate limiting (reads scope["user"])
    app = RateLimitMiddleware(app)
    if AUTH_REQUIRED:
        # RequireAuthMiddleware: rejects unauthenticated requests with 401
        app = RequireAuthMiddleware(app, required_scopes=[])
    # AuthenticationMiddleware: extracts Bearer token, calls verifier
    app = AuthenticationMiddleware(app, backend=BearerAuthBackend(verifier))

    # ── Lifespan Bypass ──
    # RequireAuthMiddleware rejects ASGI lifespan events (no user in scope).
    # Route lifespan directly to the Starlette app, HTTP through auth chain.
    auth_app = app

    class LifespanBypass:
        """Routes lifespan events past auth middleware to Starlette."""
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "lifespan":
                await starlette_app(scope, receive, send)
            else:
                await auth_app(scope, receive, send)

    app = LifespanBypass()

    mode = "enforced" if AUTH_REQUIRED else "optional"
    log.info("MCP Streamable HTTP auf %s:%s%s (auth: %s)", MCP_HOST, MCP_PORT, MCP_PATH, mode)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
