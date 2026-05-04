"""
Knowledge base MCP server
============================
Single access point for agents. Implements MCP tools for
semantic search, structured queries, rule-set access,
data ingestion, evaluation/feedback and snapshots.

Building block 3: submit_feedback, get_eval_stats (+ feedback loop in search_knowledge)
Building block 4: create_snapshot, list_snapshots
Building block 5: Prometheus metrics (/metrics HTTP on port 8080) + OpenTelemetry tracing
"""

import asyncio
import os
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent
from mcp.server.auth.provider import TokenVerifier, AccessToken
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.routes import create_auth_routes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, JSONResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route
from starlette.types import Scope, Receive, Send
from starlette.middleware.authentication import AuthenticationMiddleware

from oauth_provider import PowerbrainOAuthProvider, CombinedTokenVerifier
from login_page import render_login_page
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
from shared.rerank_provider import create_rerank_provider, RerankDocument
from shared.config import read_secret, build_postgres_url, PG_POOL_MIN, PG_POOL_MAX
from shared.opa_client import (
    OpaPolicyMissingError,
    opa_query,
    verify_required_policies,
)
from shared.ingestion_auth import verify_ingestion_auth_configured
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
RERANKER_URL     = os.getenv("RERANKER_URL",     "http://reranker:8082")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() == "true"
RERANKER_BACKEND = os.getenv("RERANKER_BACKEND", "powerbrain")
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANKER_MODEL_NAME = os.getenv("RERANKER_MODEL", "")
INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")
# B-50: defense-in-depth bearer for the ingestion service. Read once
# at startup (Docker Secret /run/secrets/ingestion_auth_token).
INGESTION_AUTH_TOKEN = read_secret("INGESTION_AUTH_TOKEN", "")
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").lower() == "true"
SKIP_INGESTION_AUTH_STARTUP_CHECK = (
    os.getenv("SKIP_INGESTION_AUTH_STARTUP_CHECK", "false").lower() == "true"
)
# Fail-closed: refuse to start with empty token + AUTH_REQUIRED=true (#126).
verify_ingestion_auth_configured(
    INGESTION_AUTH_TOKEN,
    auth_required=AUTH_REQUIRED,
    skip_check=SKIP_INGESTION_AUTH_STARTUP_CHECK,
    service_name="mcp-server",
)


def _ingestion_headers() -> dict[str, str]:
    """Auth headers for internal ingestion calls; empty when token unset."""
    return (
        {"Authorization": f"Bearer {INGESTION_AUTH_TOKEN}"}
        if INGESTION_AUTH_TOKEN
        else {}
    )

# ── Backward-compat fallback ──
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# ── Embedding provider ──
EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL        = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY      = os.getenv("EMBEDDING_API_KEY", "")

# ── LLM provider (legacy single-pool fallback) ──
# Kept as the default for SUMMARIZATION_* below so single-endpoint
# deployments keep working without env changes. The MCP server itself
# only consumes an LLM for summarization — agent-loop calls live in
# pb-proxy, on a separate provider config.
LLM_PROVIDER_URL       = os.getenv("LLM_PROVIDER_URL", _OLLAMA_URL)
LLM_MODEL              = os.getenv("LLM_MODEL", "qwen2.5:3b")
LLM_API_KEY            = os.getenv("LLM_API_KEY", "")

# ── Summarization provider (decoupled pool) ──
# Setting these lets the in-pipeline summary call route to its own
# endpoint / model so it never contends with the pb-proxy agent loop
# on a shared Ollama slot. See
# docs/plans/2026-04-20-separate-summary-llm-pool.md.
SUMMARIZATION_PROVIDER_URL = os.getenv("SUMMARIZATION_PROVIDER_URL", LLM_PROVIDER_URL)
SUMMARIZATION_MODEL        = os.getenv("SUMMARIZATION_MODEL", LLM_MODEL)
SUMMARIZATION_API_KEY      = os.getenv("SUMMARIZATION_API_KEY", LLM_API_KEY)
SUMMARIZATION_ENABLED      = os.getenv("SUMMARIZATION_ENABLED", "true").lower() == "true"
# Per-call timeout for summarization LLM requests. Kept well below the
# proxy's TOOL_CALL_TIMEOUT so the graceful fallback to raw chunks
# ([server.py] summarize_text → except → return None) lands before the
# upstream caller abandons the connection. See
# docs/playbook-sales-demo.md → "Tuning the local LLM".
SUMMARIZATION_TIMEOUT      = float(os.getenv("SUMMARIZATION_TIMEOUT", "15"))

embedding_provider     = EmbeddingProvider(base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY)
summarization_provider = CompletionProvider(base_url=SUMMARIZATION_PROVIDER_URL, api_key=SUMMARIZATION_API_KEY)
_rerank_provider   = create_rerank_provider(
    backend=RERANKER_BACKEND, base_url=RERANKER_URL,
    api_key=RERANKER_API_KEY, model=RERANKER_MODEL_NAME,
)

from shared.embedding_cache import EmbeddingCache
embedding_cache = EmbeddingCache()

MCP_HOST       = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT       = int(os.getenv("MCP_PORT", "8080"))
MCP_PATH       = os.getenv("MCP_PATH", "/mcp")
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", f"http://localhost:{MCP_PORT}")
METRICS_PORT   = int(os.getenv("METRICS_PORT", "9091"))
# AUTH_REQUIRED is already read above (next to INGESTION_AUTH_TOKEN) so the
# fail-closed boot check (#126) sees the same value.

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

# Feedback loop: warning when avg_rating is below this threshold with at least N feedbacks
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

# ── PII metadata field mapping (B-31) ───────────────────────
import yaml as _yaml

def _load_pii_metadata_fields() -> dict[str, str]:
    """Load metadata-key → OPA redaction-field mapping from pii_config.yaml."""
    for path in (
        os.getenv("PII_CONFIG_PATH", ""),
        "/app/ingestion/pii_config.yaml",          # Docker default
        os.path.join(os.path.dirname(__file__), "..", "ingestion", "pii_config.yaml"),
    ):
        if path and os.path.isfile(path):
            try:
                with open(path) as f:
                    cfg = _yaml.safe_load(f)
                return cfg.get("pii_metadata_fields") or {}
            except Exception as exc:
                logging.getLogger("pb-mcp").warning("pii_config.yaml load failed: %s", exc)
                return {}
    return {}

PII_METADATA_FIELDS: dict[str, str] = _load_pii_metadata_fields()

# ── Policy data schema (B-12) ───────────────────────────────
import jsonschema as _jsonschema


def _load_policy_schema() -> dict:
    """Load the policy data JSON Schema for manage_policies validation."""
    for path in (
        os.getenv("POLICY_SCHEMA_PATH", ""),
        "/app/policy_data_schema.json",                     # Docker default
        os.path.join(os.path.dirname(__file__), "..", "opa-policies", "pb", "policy_data_schema.json"),
    ):
        if path and os.path.isfile(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception as exc:
                logging.getLogger("pb-mcp").warning("policy_data_schema.json load failed: %s", exc)
                return {}
    return {}


_POLICY_SCHEMA: dict = _load_policy_schema()
_POLICY_SECTION_PROPS: dict[str, dict] = (
    _POLICY_SCHEMA
    .get("properties", {}).get("pb", {})
    .get("properties", {}).get("config", {})
    .get("properties", {})
) if _POLICY_SCHEMA else {}

# ── Prometheus metrics ───────────────────────────────────────
mcp_requests_total = Counter(
    "pb_mcp_requests_total",
    "MCP requests per tool and status",
    ["tool", "status"],
)
mcp_request_duration = Histogram(
    "pb_mcp_request_duration_seconds",
    "Latency per MCP tool",
    ["tool"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)
mcp_policy_decisions_total = Counter(
    "pb_mcp_policy_decisions_total",
    "OPA policy decisions",
    ["result"],
)
mcp_policy_updates_total = Counter(
    "pb_mcp_policy_updates_total",
    "Policy data updates via manage_policies",
    ["section"],
)
mcp_search_results_count = Histogram(
    "pb_mcp_search_results_count",
    "Number of search results after reranking",
    ["collection"],
    buckets=[0, 1, 3, 5, 10, 20, 50],
)
mcp_rerank_fallback_total = Counter(
    "pb_mcp_rerank_fallback_total",
    "Number of reranker fallbacks (unreachable)",
)
mcp_feedback_avg_rating = Gauge(
    "pb_feedback_avg_rating",
    "Current average of the feedback rating (last 24h)",
)

# Note: B-45 accuracy gauges (pb_accuracy_*) live in worker/metrics.py
# because the pb-worker container computes them and exposes its own
# Prometheus /metrics endpoint. Defining them here too would create
# label-set conflicts when both processes scrape into the same job.
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
        try:
            self.last_refill = asyncio.get_running_loop().time()
        except RuntimeError:
            self.last_refill = 0.0
        self._lock = asyncio.Lock()
        self.last_used = self.last_refill

    async def consume(self) -> tuple[bool, float]:
        """Try to consume a token. Returns (allowed, retry_after_seconds)."""
        async with self._lock:
            now = asyncio.get_running_loop().time()
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
        now = asyncio.get_running_loop().time()
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
            # Extract agent info from auth state (set by AuthenticationMiddleware)
            user = scope.get("user")
            if user and hasattr(user, "identity") and user.identity:
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
            # No authenticated user — skip rate limiting (auth middleware handles rejection)
        except Exception as e:
            # Fail open — rate limiter error should not block requests
            log.warning(f"Rate limiter error, request is being passed through: {e!r}")

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
    before_sleep=lambda rs: log.warning(f"Embed retry #{rs.attempt_number} after error: {rs.outcome.exception()}"),
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
                         model=SUMMARIZATION_MODEL):
        try:
            return await summarization_provider.generate(
                http,
                model=SUMMARIZATION_MODEL,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout=SUMMARIZATION_TIMEOUT,
            )
        except Exception as e:
            log.warning(f"Summarization failed, returning raw chunks: {e}")
            return None


async def check_opa_summarization_policy(
    agent_role: str,
    classification: str,
) -> dict:
    """Check OPA summarization policy. Returns {allowed, required, detail}.

    Fail-closed on missing policy or transport error: summarization is
    an extra permission, so denying by default is the safe fallback.
    """
    input_data = {
        "agent_role": agent_role,
        "classification": classification,
    }
    try:
        allowed = await opa_query(
            http, OPA_URL, "pb/summarization/summarize_allowed", input_data,
        )
        required = await opa_query(
            http, OPA_URL, "pb/summarization/summarize_required", input_data,
        )
        detail = await opa_query(
            http, OPA_URL, "pb/summarization/summarize_detail", input_data,
        )
    except OpaPolicyMissingError as exc:
        log.error(
            "OPA summarization policy not loaded (%s) — denying by default",
            exc.package_path,
        )
        return {"allowed": False, "required": False, "detail": "standard"}
    except Exception as e:
        log.warning("OPA summarization policy check failed: %s", e)
        return {"allowed": False, "required": False, "detail": "standard"}

    return {
        "allowed":  bool(allowed) if allowed is not None else False,
        "required": bool(required) if required is not None else False,
        "detail":   detail if isinstance(detail, str) else "standard",
    }


def _apply_heuristic_boosts(
    results: list[RerankDocument],
    options: dict,
) -> list[RerankDocument]:
    """Apply metadata-based score adjustments after cross-encoder reranking."""
    match_project = options.get("match_project", "")
    match_author = options.get("match_author", "")
    match_files = set(options.get("match_files", []))
    boost_project     = float(options.get("boost_same_project", 0.0))
    boost_author      = float(options.get("boost_same_author", 0.0))
    boost_files       = float(options.get("boost_file_overlap", 0.0))
    boost_corrections = float(options.get("boost_corrections", 0.0))

    for doc in results:
        meta = doc.metadata
        bonus = 0.0
        if match_project and meta.get("project") == match_project:
            bonus += boost_project
        if match_author and meta.get("userName") == match_author:
            bonus += boost_author
        if match_files and boost_files:
            doc_files = set(meta.get("files", []))
            if doc_files & match_files:
                overlap_ratio = len(doc_files & match_files) / len(match_files)
                bonus += boost_files * overlap_ratio
        if boost_corrections and meta.get("isCorrection"):
            bonus += boost_corrections
        doc.rerank_score += bonus
    return results


async def rerank_results(query: str, documents: list[dict], top_n: int,
                         rerank_options: dict | None = None) -> list[dict]:
    if not RERANKER_ENABLED or not documents:
        return documents[:top_n]

    with trace_operation(tracer, "reranking", "mcp-server",
                         input_count=len(documents), top_n=top_n):
        try:
            docs = [
                RerankDocument(
                    id=d["id"],
                    content=d.get("metadata", {}).get("rerank_content") or d["content"],
                    score=d.get("score", 0.0), metadata=d.get("metadata", {}),
                )
                for d in documents
            ]
            results = await _rerank_provider.rerank(http, query, docs, top_n)
            if rerank_options:
                results = _apply_heuristic_boosts(results, rerank_options)
                results.sort(key=lambda r: r.rerank_score, reverse=True)
                for i, r in enumerate(results):
                    r.rank = i + 1
            return [r.to_dict() for r in results]
        except Exception as e:
            log.warning(f"Reranker not reachable, using original order: {e}")
            mcp_rerank_fallback_total.inc()
            return documents[:top_n]


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
    before_sleep=lambda rs: log.warning(f"OPA retry #{rs.attempt_number} after error: {rs.outcome.exception()}"),
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
            result = await opa_query(
                http, OPA_URL, "pb/access/allow", input_data,
            )
            allowed = bool(result)
        except (httpx.ConnectError, httpx.TimeoutException):
            raise  # Let tenacity retry these
        except OpaPolicyMissingError as exc:
            # The access gate is the MOST critical OPA policy. If it's
            # not loaded, we MUST fail-closed — a silent default of
            # "deny" is correct, but surface the cause loudly so
            # operators can fix the misconfiguration.
            log.error(
                "OPA access policy %s not loaded — denying all requests. "
                "Fix OPA config and restart.", exc.package_path,
            )
            allowed = False
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
                    scan_resp = await http.post(
                        f"{INGESTION_URL}/scan",
                        json={"text": context["query"]},
                        headers=_ingestion_headers(),
                    )
                    scan_resp.raise_for_status()
                    break
                except (httpx.ConnectError, httpx.TimeoutException):
                    if attempt == 0:
                        log.warning("PII scan retry after connection error...")
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
            log.warning(f"PII scan for audit log failed, saving without scan: {e}")
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


def _parse_iso_datetime(s, *, field: str):
    """Parse an ISO-8601 datetime string into an aware `datetime`.

    Accepts the variants commonly emitted by clients:
      * `2026-04-30T00:00:00Z`         (Z suffix)
      * `2026-04-30T00:00:00+00:00`    (explicit offset)
      * `2026-04-30T00:00:00`          (naive — assumed UTC)

    Returns None for None/empty input. Raises ValueError with the
    field name and the offending value on parse failure (so callers
    can surface a 422-style error to the client).
    """
    from datetime import datetime, timezone
    if not s:
        return None
    try:
        # Python 3.11+ accepts "Z" natively, but normalise anyway.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError) as e:
        raise ValueError(f"{field}: invalid ISO-8601 datetime: {s!r} ({e})")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_pii_access_token(token: dict) -> dict:
    """
    Validates a PII access token (HMAC-signed, short-lived).
    Returns: {"valid": bool, "reason": str, "payload": dict}
    """
    import hmac as hmac_mod
    import hashlib
    from datetime import datetime, timezone

    signature = token.get("signature", "")
    payload = {k: v for k, v in token.items() if k != "signature"}

    # Verify HMAC signature
    expected = hmac_mod.new(
        VAULT_HMAC_SECRET.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac_mod.compare_digest(signature, expected):
        return {"valid": False, "reason": "Invalid token signature", "payload": payload}

    # Check expiration
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
    """Checks via OPA whether vault access is allowed.

    Fail-closed on missing policy: the vault holds original PII, so a
    silent default of "allow" would be unacceptable.
    """
    input_data = {
        "agent_role": agent_role,
        "purpose": purpose,
        "classification": classification,
        "data_category": data_category,
        "token_valid": token_valid,
        "token_expired": token_expired,
    }
    try:
        allowed_raw = await opa_query(
            http, OPA_URL, "pb/privacy/vault_access_allowed", input_data,
        )
        fields_raw = await opa_query(
            http, OPA_URL, "pb/privacy/vault_fields_to_redact", input_data,
        )
        allowed = bool(allowed_raw)
        fields_to_redact = list(fields_raw) if isinstance(fields_raw, list) else []
    except OpaPolicyMissingError as exc:
        log.error(
            "OPA vault policy not loaded (%s) — denying vault access",
            exc.package_path,
        )
        allowed = False
        fields_to_redact = []
    except Exception as e:
        log.warning(f"OPA vault access check failed: {e}")
        allowed = False
        fields_to_redact = []
    return {
        "allowed": allowed,
        "fields_to_redact": fields_to_redact,
    }


def redact_fields(text: str, pii_entities: list[dict], fields_to_redact: set[str]) -> str:
    """Redacts specific PII entity types in the text based on OPA policy."""
    # Mapping from OPA field names to one or more Presidio entity types.
    # Custom recognizers (e.g. DE_DATE_OF_BIRTH) map to the same category as
    # their built-in counterpart so purpose-based redaction stays consistent.
    field_to_entities: dict[str, tuple[str, ...]] = {
        "email":     ("EMAIL_ADDRESS",),
        "phone":     ("PHONE_NUMBER",),
        "iban":      ("IBAN_CODE",),
        "birthdate": ("DATE_OF_BIRTH", "DE_DATE_OF_BIRTH"),
        "address":   ("LOCATION",),
        "person":    ("PERSON",),
    }
    entities_to_redact: set[str] = set()
    for f in fields_to_redact:
        entities_to_redact.update(field_to_entities.get(f, ()))

    if not entities_to_redact:
        return text

    # Sort by position descending for stable offsets
    sorted_entities = sorted(pii_entities, key=lambda e: e.get("start", 0), reverse=True)
    result = text
    for entity in sorted_entities:
        if entity.get("type") in entities_to_redact:
            start = entity.get("start", 0)
            end = entity.get("end", 0)
            if 0 <= start < end <= len(result):
                result = result[:start] + f"<{entity['type']}>" + result[end:]
    return result


# ── B-30: PII masking for graph query results ──────────────
#
# Graph properties carry their semantics in the key name — a key called
# "email" is an email, period. That's a deterministic classification, so
# we don't need (and shouldn't pay for) per-value Presidio NER:
#
#   * Presidio is probabilistic. spaCy-DE flagged "Elena_Hartmann" as
#     PERSON, "Tim_Heller" below threshold, "Sarah_Bach" as LOCATION —
#     producing visually inconsistent output across a single graph.
#   * Every call crossed the Docker network to ingestion/scan, making
#     graph_query latency depend on an unrelated service.
#
# We replace this with a config-driven key → entity-type-tag mapping
# loaded from data.pb.config.graph_pii_keys. The result is fully
# deterministic, audit-friendly, and editable at runtime via the
# manage_policies MCP tool.

_graph_pii_keys_cache: dict[str, str] = {}
_graph_pii_keys_loaded = False


async def _get_graph_pii_keys() -> dict[str, str]:
    """Load the graph-key → entity-type-tag mapping from OPA config.

    Cached for the process lifetime. Use manage_policies (which clears the
    OPA cache) or a process restart to pick up edits. Graceful fallback to
    the hardcoded default if OPA is unreachable at startup.
    """
    global _graph_pii_keys_loaded, _graph_pii_keys_cache
    if _graph_pii_keys_loaded:
        return _graph_pii_keys_cache

    default = {
        "name":      "PERSON",
        "fullname":  "PERSON",
        "firstname": "PERSON",
        "lastname":  "PERSON",
        "email":     "EMAIL_ADDRESS",
        "phone":     "PHONE_NUMBER",
    }
    try:
        resp = await http.get(f"{OPA_URL}/v1/data/pb/config/graph_pii_keys")
        resp.raise_for_status()
        cfg = resp.json().get("result")
        if isinstance(cfg, dict) and cfg:
            _graph_pii_keys_cache = {k.lower(): v for k, v in cfg.items()}
        else:
            _graph_pii_keys_cache = default
    except Exception as exc:
        log.warning("OPA graph_pii_keys load failed, using default: %s", exc)
        _graph_pii_keys_cache = default
    _graph_pii_keys_loaded = True
    return _graph_pii_keys_cache


async def _mask_graph_pii(data: Any) -> Any:
    """Recursively mask PII in graph query result dicts.

    Walks dicts/lists. String values whose lower-cased key matches a
    configured graph_pii_keys entry are replaced with ``<ENTITY_TYPE>``.
    """
    keys = await _get_graph_pii_keys()
    return _mask_walk(data, keys)


def _mask_walk(data: Any, keys: dict[str, str]) -> Any:
    if isinstance(data, list):
        return [_mask_walk(item, keys) for item in data]
    if not isinstance(data, dict):
        return data

    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str) and value:
            entity = keys.get(key.lower())
            result[key] = f"<{entity}>" if entity else value
        elif isinstance(value, (dict, list)):
            result[key] = _mask_walk(value, keys)
        else:
            result[key] = value
    return result


# ── B-31: Metadata PII redaction for search results ────────

_fields_to_redact_cache: _TTLCache[str, set[str]] = _TTLCache(maxsize=32, ttl=OPA_CACHE_TTL)


async def _get_fields_to_redact(purpose: str) -> set[str]:
    """Get the set of fields to redact for a purpose from OPA (cached)."""
    cache_key = f"redact:{purpose or 'default'}"
    with _opa_cache_lock:
        cached = _fields_to_redact_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = await http.post(
            f"{OPA_URL}/v1/data/pb/privacy/fields_to_redact",
            json={"input": {"purpose": purpose or "default"}},
        )
        resp.raise_for_status()
        fields = set(resp.json().get("result", []))
    except Exception as exc:
        log.warning("OPA fields_to_redact query failed, using default: %s", exc)
        fields = set(["email", "phone", "iban", "birthdate", "address", "person"])

    with _opa_cache_lock:
        _fields_to_redact_cache[cache_key] = fields
    return fields


async def _redact_metadata_pii(metadata: dict, purpose: str) -> dict:
    """Redact PII-sensitive metadata keys based on OPA fields_to_redact policy.

    Checks each metadata key against PII_METADATA_FIELDS mapping.
    If the mapped redaction category is in the fields_to_redact set,
    replaces the value with a <REDACTED> placeholder.
    """
    if not PII_METADATA_FIELDS:
        return metadata

    redact_fields_set = await _get_fields_to_redact(purpose)
    redacted = dict(metadata)
    for key, value in metadata.items():
        category = PII_METADATA_FIELDS.get(key)
        if category and category in redact_fields_set and value:
            redacted[key] = "<REDACTED>"
    return redacted


async def vault_lookup(
    document_id: str, chunk_indices: list[int] | None = None
) -> list[dict]:
    """Retrieves original data from the vault."""
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
    """Logs vault access into a separate audit log."""
    pool = await get_pg_pool()
    await pool.execute("""
        INSERT INTO pii_vault.vault_access_log
            (agent_id, document_id, chunk_index, purpose, token_hash)
        VALUES ($1, $2, $3, $4, $5)
    """, agent_id, document_id, chunk_index, purpose, token_hash)


# ── Text-level vault resolution (pb-proxy enterprise edition) ────────────

# Regex matching the pseudonyms emitted by ingestion's
# pseudonymize_text(): [ENTITY_TYPE:8-hex-chars].
_VAULT_PSEUDONYM_RE = re.compile(r"\[([A-Z_]+):([a-f0-9]{8})\]")


async def vault_resolve_pseudonyms(
    text: str,
    *,
    purpose: str,
    agent_role: str,
    agent_id: str,
    token_hash: str,
) -> dict:
    """Resolve ``[ENTITY_TYPE:hash]`` pseudonyms in ``text`` back to originals.

    Callers (notably pb-proxy's agent loop) hand in a tool result that may
    contain pseudonyms produced during ingestion. This function:

    1. Extracts every pseudonym via the canonical regex.
    2. Batches a single ``pseudonym_mapping`` lookup to resolve each one
       to ``(document_id, chunk_index, salt, entity_type)``.
    3. Loads the referenced vault chunks once per ``(document_id,
       chunk_index)`` pair and hashes each entity in ``pii_entities``
       against the requested pseudonym hash to recover the original value.
    4. Gates each resolution through ``pb.privacy.vault_access_allowed``
       using the document's classification and data_category — silently
       skipping resolutions whose OPA decision denies access.
    5. Applies ``pb.privacy.vault_fields_to_redact`` for the purpose, so
       purposes like ``billing`` still blank IBAN/address fields even when
       a resolution is allowed.
    6. Logs every *successful* resolution via ``log_vault_access`` so the
       audit chain matches the search_knowledge vault path.

    Returns a dict summarising the outcome — useful for diagnostics and
    for surfaces like the demo that want to show counts without digging
    into audit tables.
    """
    if not text:
        return {"text": text, "resolved": 0, "total": 0, "skipped": 0}

    matches = list(_VAULT_PSEUDONYM_RE.finditer(text))
    if not matches:
        return {"text": text, "resolved": 0, "total": 0, "skipped": 0}

    pseudonyms = list({m.group(0) for m in matches})

    pool = await get_pg_pool()
    mapping_rows = await pool.fetch("""
        SELECT pm.pseudonym, pm.document_id, pm.chunk_index,
               pm.entity_type, pm.salt,
               oc.original_text, oc.pii_entities,
               dm.classification, dm.metadata
        FROM pii_vault.pseudonym_mapping pm
        JOIN pii_vault.original_content oc
             ON pm.document_id = oc.document_id
            AND pm.chunk_index = oc.chunk_index
        JOIN documents_meta dm
             ON dm.id = pm.document_id
        WHERE pm.pseudonym = ANY($1)
    """, pseudonyms)

    # Cache of pseudonym → original string (only for resolutions the
    # policy allowed). This is what we substitute back into the text.
    resolved: dict[str, str] = {}
    # Per-(doc_id, chunk_index) we only log once regardless of how many
    # pseudonyms landed on the same chunk.
    audited: set[tuple[str, int]] = set()

    for row in mapping_rows:
        pseudonym = row["pseudonym"]
        if pseudonym in resolved:
            continue  # already handled via an earlier mapping row

        doc_id = str(row["document_id"])
        chunk_index = row["chunk_index"]
        classification = row["classification"] or "internal"

        # The document's data_category lives in metadata JSONB alongside
        # other ingestion hints. Default empty → OPA purpose-binding fails.
        meta = row["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        data_category = meta.get("data_category", "")

        vault_policy = await check_opa_vault_access(
            agent_role, purpose, classification,
            data_category, True, False,
        )
        if not vault_policy.get("allowed"):
            continue

        # Reverse-hash the pseudonym against the entities stored with the
        # chunk. Entities are {type, text, start, end, score}; only the
        # ``text`` field produces the pseudonym's hash for this salt.
        entities = row["pii_entities"]
        if isinstance(entities, str):
            try:
                entities = json.loads(entities)
            except json.JSONDecodeError:
                entities = []
        salt = row["salt"]
        target_type = row["entity_type"]
        target_hash = pseudonym.rsplit(":", 1)[-1].rstrip("]")

        recovered = None
        for ent in entities or []:
            if ent.get("type") != target_type:
                continue
            candidate = ent.get("text", "")
            digest = hashlib.sha256(
                f"{salt}:{candidate}".encode()
            ).hexdigest()[:8]
            if digest == target_hash:
                recovered = candidate
                break

        if recovered is None:
            continue

        # Apply purpose-based field redaction. The mapping PERSON →
        # `person`, EMAIL_ADDRESS → `email` etc. lives in redact_fields's
        # field_to_entities table — we reuse it by asking: does the
        # policy say to redact this entity type?
        fields_to_redact = set(vault_policy.get("fields_to_redact") or [])
        if _entity_type_in_redact_set(target_type, fields_to_redact):
            continue  # policy says: stay pseudonymous for this field

        resolved[pseudonym] = recovered
        key = (doc_id, chunk_index)
        if key not in audited:
            audited.add(key)
            await log_vault_access(agent_id, doc_id, chunk_index,
                                   purpose, token_hash)

    if not resolved:
        return {"text": text, "resolved": 0, "total": len(pseudonyms),
                "skipped": len(pseudonyms)}

    # Longest-first replacement avoids clashing suffix hashes eating each
    # other mid-substitution.
    out = text
    for pseudo in sorted(resolved, key=len, reverse=True):
        out = out.replace(pseudo, resolved[pseudo])

    return {
        "text":      out,
        "resolved":  len(resolved),
        "total":     len(pseudonyms),
        "skipped":   len(pseudonyms) - len(resolved),
    }


def _entity_type_in_redact_set(entity_type: str, fields: set[str]) -> bool:
    """Does the OPA ``fields_to_redact`` list cover this entity type?"""
    if not fields:
        return False
    # Invert the mapping used by redact_fields() so we can ask
    # "should I keep this pseudonym for this purpose?".
    entity_to_field = {
        "EMAIL_ADDRESS":     "email",
        "PHONE_NUMBER":      "phone",
        "IBAN_CODE":         "iban",
        "DATE_OF_BIRTH":     "birthdate",
        "DE_DATE_OF_BIRTH":  "birthdate",
        "LOCATION":          "address",
        "PERSON":            "person",
    }
    field = entity_to_field.get(entity_type)
    return field is not None and field in fields


async def check_feedback_warning(query: str, pool: asyncpg.Pool):
    """Warns when a query is frequently rated poorly (feedback loop)."""
    row = await pool.fetchrow("""
        SELECT COUNT(*) AS cnt, AVG(rating) AS avg_rating
        FROM search_feedback
        WHERE query = $1
    """, query)
    if row and row["cnt"] >= FEEDBACK_WARN_MIN_COUNT:
        avg = float(row["avg_rating"])
        if avg < FEEDBACK_WARN_THRESHOLD:
            log.warning(
                f"[Feedback loop] Query '{query[:80]}' has avg_rating={avg:.2f} "
                f"with {row['cnt']} feedbacks → check retrieval quality"
            )


# ── MCP-Server ───────────────────────────────────────────────
server = Server("pb-mcp-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_knowledge",
            description="Semantic search over the knowledge base. "
                        "Finds relevant documents, code snippets, and rules.",
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
                    "rerank_query": {
                        "type": "string",
                        "description": "Enriched query text used only for reranking, not for embedding. "
                                       "If omitted, the regular 'query' is used for both.",
                    },
                    "rerank_options": {
                        "type": "object",
                        "description": "Heuristic boost config for post-rerank score adjustment.",
                        "properties": {
                            "boost_same_project": {"type": "number", "default": 0},
                            "boost_same_author":  {"type": "number", "default": 0},
                            "match_project":      {"type": "string"},
                            "match_author":       {"type": "string"},
                            "boost_file_overlap": {"type": "number", "default": 0},
                            "match_files":        {"type": "array", "items": {"type": "string"}},
                            "boost_corrections":  {"type": "number", "default": 0,
                                                   "description": "Score boost for user-corrected documents (isCorrection metadata)"},
                        },
                    },
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="query_data",
            description="Structured query against PostgreSQL datasets.",
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
            description="Retrieve active business rules for a context.",
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
            description="Evaluate OPA policy.",
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
            description="Ingest new data into the knowledge base.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source":         {"type": "string"},
                    "source_type":    {"type": "string", "default": "text",
                                       "description": "Source type (text). Additional types via adapters."},
                    "project":        {"type": "string"},
                    "classification": {"type": "string", "default": "internal"},
                    "metadata":       {"type": "object"},
                },
                "required": ["source"]
            }
        ),
        Tool(
            name="get_classification",
            description="Query the classification of a data object.",
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
            description="List available datasets.",
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
            description="Retrieve code context from repos. Semantic search over code embeddings.",
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
            description="Query the knowledge graph (nodes, relationships, paths).",
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
            description="Mutate the knowledge graph (developer/admin only).",
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
        # ── Building block 3: Evaluation + Feedback ────────────
        Tool(
            name="submit_feedback",
            description="Submit feedback on search results. "
                        "Rates the quality of a search (1–5 stars).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query":          {"type": "string", "description": "The original search query"},
                    "result_ids":     {"type": "array", "items": {"type": "string"},
                                       "description": "IDs of the received results"},
                    "rating":         {"type": "integer", "minimum": 1, "maximum": 5,
                                       "description": "Overall rating (1=poor, 5=excellent)"},
                    "relevant_ids":   {"type": "array", "items": {"type": "string"},
                                       "description": "IDs of helpful results"},
                    "irrelevant_ids": {"type": "array", "items": {"type": "string"},
                                       "description": "IDs of unhelpful results"},
                    "comment":        {"type": "string", "description": "Free-text comment"},
                    "collection":     {"type": "string"},
                    "rerank_scores":  {"type": "object"},
                },
                "required": ["query", "result_ids", "rating"]
            }
        ),
        Tool(
            name="get_eval_stats",
            description="Retrieve statistics on retrieval quality. "
                        "Shows avg_rating, worst queries and trend.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days":       {"type": "integer", "default": 30,
                                   "description": "Evaluation period in days"},
                },
                "required": []
            }
        ),
        # ── Building block 4: Snapshots ─────────────────────────
        Tool(
            name="create_snapshot",
            description="Create a knowledge snapshot (Qdrant + PG + OPA policy commit). "
                        "Admin only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "Snapshot name (e.g. 'before-migration-v2')"},
                    "description": {"type": "string"},
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="list_snapshots",
            description="List available knowledge snapshots.",
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
        # ── EU AI Act Art. 11: Compliance Documentation ──────────
        Tool(
            name="generate_compliance_doc",
            description="Generate the EU AI Act Annex IV technical "
                        "documentation as Markdown (admin only). Aggregates "
                        "the live transparency report, risk health, "
                        "accuracy metrics, and the project risk register "
                        "into a single self-contained document.",
            inputSchema={
                "type": "object",
                "properties": {
                    "output_mode": {"type": "string",
                                    "enum": ["inline", "file"],
                                    "default": "inline",
                                    "description": "inline returns the full Markdown in the response; file writes to COMPLIANCE_DOC_DIR and returns the path"},
                },
                "required": []
            }
        ),
        # ── EU AI Act Art. 14: Human Oversight ───────────────────
        Tool(
            name="review_pending",
            description="List or decide pending human-oversight reviews "
                        "(EU AI Act Art. 14). Admin only. Without action, "
                        "returns open pending reviews. With action=approve/"
                        "deny + review_id, decides a single review.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action":    {"type": "string", "enum": ["list", "approve", "deny"],
                                  "default": "list"},
                    "review_id": {"type": "string",
                                  "description": "UUID of the review to decide (approve/deny only)"},
                    "reason":    {"type": "string",
                                  "description": "Optional justification for the decision"},
                    "limit":     {"type": "integer", "default": 50,
                                  "description": "Max rows for list action"},
                },
                "required": []
            }
        ),
        Tool(
            name="get_review_status",
            description="Poll the status of a pending human-oversight review "
                        "(EU AI Act Art. 14). Returns the current status and "
                        "(if approved) the queued tool result when available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "review_id": {"type": "string",
                                  "description": "UUID returned by the original tool call"},
                },
                "required": ["review_id"]
            }
        ),
        # ── EU AI Act Art. 13: Transparency ──────────────────────
        Tool(
            name="get_system_info",
            description="Return the transparency report (EU AI Act Art. 13): "
                        "active models, OPA policy snapshot, Qdrant collection "
                        "stats, PII scanner config, audit-chain integrity. "
                        "Same content as GET /transparency. Auth required.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        # ── EU AI Act Art. 12: Audit Hash-Chain ──────────────────
        Tool(
            name="verify_audit_integrity",
            description="Verify the tamper-evident hash chain of the audit log "
                        "(EU AI Act Art. 12). Admin only. Optional id range; "
                        "omit to verify the full current chain. Returns "
                        "valid/first_invalid_id/total_checked.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_id": {"type": "integer",
                                 "description": "First id to verify (default: from chain start or last archive checkpoint)"},
                    "end_id":   {"type": "integer",
                                 "description": "Last id to verify (default: current tail)"},
                },
                "required": []
            }
        ),
        Tool(
            name="export_audit_log",
            description="Export audit-log entries for compliance review "
                        "(EU AI Act Art. 12). Admin only. Supports JSON/CSV, "
                        "filter by time range, agent_id, action. Limited by "
                        "audit.export_max_rows in OPA data.json.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format":   {"type": "string", "enum": ["json", "csv"], "default": "json"},
                    "since":    {"type": "string", "format": "date-time",
                                 "description": "ISO-8601 lower bound on created_at (inclusive)"},
                    "until":    {"type": "string", "format": "date-time",
                                 "description": "ISO-8601 upper bound on created_at (exclusive)"},
                    "agent_id": {"type": "string",
                                 "description": "Filter by exact agent_id"},
                    "action":   {"type": "string",
                                 "description": "Filter by exact action (search, query, ingest, ...)"},
                    "limit":    {"type": "integer",
                                 "description": "Max rows (capped by audit.export_max_rows)"},
                },
                "required": []
            }
        ),
        # ── Bulk Delete: delete_documents ──────────────────────────
        Tool(
            name="delete_documents",
            description="Bulk-delete documents by filter (for import workflows). "
                        "Deletes from Qdrant, PostgreSQL, PII Vault (cascade), and Knowledge Graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_type": {"type": "string",
                                    "description": "Filter by source_type (e.g. 'document', 'git-commit')"},
                    "project":     {"type": "string",
                                    "description": "Filter by project ID"},
                    "confirm":     {"type": "boolean",
                                    "description": "Safety flag, must be true"},
                    "delete_all":  {"type": "boolean", "default": False,
                                    "description": "If true, delete ALL documents (ignores other filters)"},
                },
                "required": ["confirm"]
            }
        ),
        Tool(
            name="manage_policies",
            description="Read or update OPA policy data sections. Admin only. "
                        "Use action 'list' to see available sections, 'read' to get a section, "
                        "'update' to modify a section (validates against JSON Schema before write).",
            inputSchema={
                "type": "object",
                "properties": {
                    "action":  {"type": "string", "enum": ["list", "read", "update"],
                                "description": "Operation: list sections, read one, or update one"},
                    "section": {"type": "string",
                                "description": "Config section name (required for read/update)"},
                    "data":    {"description": "New value for the section (required for update)"},
                },
                "required": ["action"]
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
                log.error(f"Tool {name} failed: {e}", exc_info=True)
                status = "error"
                result = [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elapsed = time.perf_counter() - t_start
    mcp_requests_total.labels(tool=name, status=status).inc()
    mcp_request_duration.labels(tool=name).observe(elapsed)

    return result


def _build_delete_filter(source_type: str | None, project: str | None,
                         delete_all: bool) -> tuple[Filter | None, str, list]:
    """Build matching Qdrant filter and PG WHERE clause for delete_documents."""
    if delete_all:
        return None, "1=1", []
    must: list[FieldCondition] = []
    where, params, idx = "1=1", [], 1
    if source_type:
        must.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))
        where += f" AND source_type = ${idx}"
        params.append(source_type)
        idx += 1
    if project:
        must.append(FieldCondition(key="project", match=MatchValue(value=project)))
        where += f" AND project = ${idx}"
        params.append(project)
    qf = Filter(must=must) if must else None
    return qf, where, params


# ── B-42: Human Oversight (Art. 14) — circuit breaker + approval queue
# Tools that count as "data retrieval" and are gated by the kill-switch
# and the approval queue. All other tools (verify_audit_integrity,
# review_pending, get_system_info, …) are operator/oversight endpoints
# and must keep working when the breaker is engaged so admins can
# inspect state and flip the switch back.
_OVERSIGHT_DATA_TOOLS = {
    "search_knowledge", "query_data", "get_code_context", "get_document",
}

# Tiny in-process cache (5s TTL) so we do not hit PG on every request.
_CIRCUIT_BREAKER_CACHE: dict[str, Any] = {"state": None, "fetched_at": 0.0}
CIRCUIT_BREAKER_CACHE_TTL = 5.0


async def _get_circuit_breaker_state() -> dict:
    """Return ``{"active": bool, "reason": str | None, "set_by": str | None,
    "set_at": ISO str | None}`` with short-TTL caching."""
    import time as _time
    now = _time.time()
    cached = _CIRCUIT_BREAKER_CACHE["state"]
    if cached is not None and (now - _CIRCUIT_BREAKER_CACHE["fetched_at"]) < CIRCUIT_BREAKER_CACHE_TTL:
        return cached

    try:
        pool = await get_pg_pool()
        row = await pool.fetchrow(
            "SELECT active, reason, set_by, set_at "
            "FROM pb_circuit_breaker_state WHERE id = 1"
        )
        if row is None:
            state = {"active": False, "reason": None, "set_by": None, "set_at": None}
        else:
            state = {
                "active": bool(row["active"]),
                "reason": row["reason"],
                "set_by": row["set_by"],
                "set_at": row["set_at"].isoformat() if row["set_at"] else None,
            }
    except Exception as e:
        log.warning(f"Circuit breaker state fetch failed, fail-open: {e}")
        state = {"active": False, "reason": None, "set_by": None, "set_at": None}

    _CIRCUIT_BREAKER_CACHE["state"] = state
    _CIRCUIT_BREAKER_CACHE["fetched_at"] = now
    return state


def _invalidate_circuit_breaker_cache() -> None:
    _CIRCUIT_BREAKER_CACHE["state"] = None
    _CIRCUIT_BREAKER_CACHE["fetched_at"] = 0.0


async def _set_circuit_breaker(active: bool, reason: str | None,
                               set_by: str) -> dict:
    """Update the kill-switch row and invalidate the cache."""
    pool = await get_pg_pool()
    await pool.execute(
        "UPDATE pb_circuit_breaker_state "
        "SET active = $1, reason = $2, set_by = $3, set_at = now() "
        "WHERE id = 1",
        active, reason, set_by,
    )
    _invalidate_circuit_breaker_cache()
    return await _get_circuit_breaker_state()


async def _check_oversight_approval(classification: str,
                                    agent_role: str, tool: str) -> dict:
    """Query OPA ``pb.oversight.requires_approval`` and ``approval_reason``.

    Returns ``{"required": bool, "reason": str, "timeout_minutes": int}``.
    On OPA failure we fail-open (required=False) so an OPA outage cannot
    block everything; the circuit breaker exists for that case.
    """
    input_data = {
        "agent_role":     agent_role,
        "classification": classification,
        "tool":           tool,
    }
    try:
        r1, r2, r3 = await asyncio.gather(
            http.post(f"{OPA_URL}/v1/data/pb/oversight/requires_approval",
                      json={"input": input_data}, timeout=2.0),
            http.post(f"{OPA_URL}/v1/data/pb/oversight/approval_reason",
                      json={"input": input_data}, timeout=2.0),
            http.get(f"{OPA_URL}/v1/data/pb/oversight/pending_review_timeout_minutes",
                     timeout=2.0),
        )
        for r in (r1, r2, r3):
            r.raise_for_status()
        required        = bool(r1.json().get("result", False))
        reason          = r2.json().get("result", "") or ""
        timeout_minutes = int(r3.json().get("result", 60) or 60)
    except Exception as e:
        log.warning(f"OPA oversight check failed, fail-open: {e}")
        return {"required": False, "reason": "", "timeout_minutes": 60}

    # Clamp to sane bounds: 1 minute (immediate) to 24 hours (long review)
    timeout_minutes = max(1, min(1440, timeout_minutes))
    return {"required": required, "reason": reason, "timeout_minutes": timeout_minutes}


async def _create_pending_review(agent_id: str, agent_role: str, tool: str,
                                 arguments: dict, classification: str,
                                 reason: str, timeout_minutes: int) -> str:
    """Insert a pending_reviews row and return its UUID."""
    pool = await get_pg_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO pending_reviews
            (agent_id, agent_role, tool, arguments, classification,
             status, reason, expires_at)
        VALUES ($1, $2, $3, $4, $5, 'pending', $6,
                now() + make_interval(mins => $7))
        RETURNING id::text, expires_at
        """,
        agent_id, agent_role, tool, json.dumps(arguments),
        classification, reason, timeout_minutes,
    )
    return row["id"]


async def _dispatch(name: str, arguments: dict[str, Any],
                    agent_id: str, agent_role: str) -> list[TextContent]:

    # ── B-42 Circuit Breaker: block data tools when active ──
    if name in _OVERSIGHT_DATA_TOOLS:
        cb = await _get_circuit_breaker_state()
        if cb["active"]:
            await log_access(agent_id, agent_role, "oversight", "circuit_breaker",
                             name, "deny",
                             {"reason": cb.get("reason")})
            return [TextContent(type="text", text=json.dumps({
                "error": "circuit_breaker_active",
                "reason": cb.get("reason") or "human oversight kill-switch engaged",
                "set_by": cb.get("set_by"),
                "set_at": cb.get("set_at"),
            }))]

    # ── B-42 Approval Queue: intercept searches on sensitive data ──
    if name in _OVERSIGHT_DATA_TOOLS and agent_role != "admin":
        # Only search_knowledge + get_code_context + get_document carry
        # an explicit classification filter; query_data is OPA-gated
        # separately. For search-path we peek at the filters for the
        # classification hint; default to "internal" when unspecified.
        classification = (
            (arguments.get("filters") or {}).get("classification")
            or arguments.get("classification")
            or "internal"
        )
        approval = await _check_oversight_approval(classification, agent_role, name)
        if approval["required"]:
            review_id = await _create_pending_review(
                agent_id, agent_role, name, arguments,
                classification, approval["reason"],
                approval["timeout_minutes"],
            )
            await log_access(agent_id, agent_role, "oversight", "pending",
                             name, "allow",
                             {"review_id": review_id,
                              "classification": classification})
            return [TextContent(type="text", text=json.dumps({
                "status":     "pending",
                "review_id":  review_id,
                "reason":     approval["reason"],
                "poll_tool":  "get_review_status",
                "timeout_minutes": approval["timeout_minutes"],
            }, ensure_ascii=False, indent=2))]

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

        rerank_q = arguments.get("rerank_query") or query
        rerank_opts = arguments.get("rerank_options")
        reranked = await rerank_results(rerank_q, filtered, top_n=top_k,
                                        rerank_options=rerank_opts)
        mcp_search_results_count.labels(collection=collection).observe(len(reranked))

        # B-31: redact PII-sensitive metadata fields based on purpose policy
        if PII_METADATA_FIELDS:
            for item in reranked:
                if item.get("metadata"):
                    item["metadata"] = await _redact_metadata_pii(
                        item["metadata"], purpose)

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
                text=json.dumps({"error": f"Dataset '{dataset}' not found"}))]

        policy = await check_opa_policy(agent_id, agent_role,
                                        f"dataset/{ds['id']}", ds["classification"])
        if not policy["allowed"]:
            await log_access(agent_id, agent_role, "dataset", str(ds["id"]), "query", "deny")
            return [TextContent(type="text",
                text=json.dumps({"error": "Access denied", "classification": ds["classification"]}))]

        where_clauses = ["dataset_id = $1"]
        params: list[Any] = [ds["id"]]
        idx = 2
        for key, value in conditions.items():
            if not validate_identifier(key):
                return [TextContent(type="text",
                    text=json.dumps({"error": f"Invalid condition key: {key!r}"}))]
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

    # ── manage_policies (B-12) ──────────────────────────────
    elif name == "manage_policies":
        if agent_role != "admin":
            await log_access(agent_id, agent_role, "policy", "manage_policies",
                             "manage_policies", "deny")
            return [TextContent(type="text",
                text=json.dumps({"error": "manage_policies requires admin role"}))]

        action = arguments["action"]

        if action == "list":
            sections = {}
            for name_s, schema in _POLICY_SECTION_PROPS.items():
                sections[name_s] = schema.get("description", "")
            await log_access(agent_id, agent_role, "policy", "config",
                             "manage_policies.list", "allow")
            return [TextContent(type="text",
                text=json.dumps({"sections": sections}, ensure_ascii=False, indent=2))]

        section = arguments.get("section", "")
        if not section or section not in _POLICY_SECTION_PROPS:
            valid = sorted(_POLICY_SECTION_PROPS.keys())
            return [TextContent(type="text",
                text=json.dumps({"error": f"Unknown section: {section!r}",
                                 "valid_sections": valid}))]

        if action == "read":
            try:
                resp = await http.get(f"{OPA_URL}/v1/data/pb/config/{section}")
                resp.raise_for_status()
                value = resp.json().get("result")
            except Exception as exc:
                return [TextContent(type="text",
                    text=json.dumps({"error": f"Failed to read section: {exc}"}))]
            await log_access(agent_id, agent_role, "policy", section,
                             "manage_policies.read", "allow")
            return [TextContent(type="text",
                text=json.dumps({"section": section, "data": value},
                                ensure_ascii=False, indent=2))]

        if action == "update":
            new_data = arguments.get("data")
            if new_data is None:
                return [TextContent(type="text",
                    text=json.dumps({"error": "Missing 'data' for update action"}))]

            # Validate against per-section JSON Schema
            section_schema = _POLICY_SECTION_PROPS.get(section, {})
            if section_schema:
                try:
                    _jsonschema.validate(instance=new_data, schema=section_schema)
                except _jsonschema.ValidationError as ve:
                    return [TextContent(type="text",
                        text=json.dumps({"error": "Schema validation failed",
                                         "detail": ve.message,
                                         "path": list(ve.absolute_path)}))]

            # Read current value for audit context
            try:
                old_resp = await http.get(f"{OPA_URL}/v1/data/pb/config/{section}")
                old_resp.raise_for_status()
                old_value = old_resp.json().get("result")
            except Exception:
                old_value = None

            # Write to OPA Data API
            try:
                put_resp = await http.put(
                    f"{OPA_URL}/v1/data/pb/config/{section}",
                    json=new_data,
                )
                put_resp.raise_for_status()
            except Exception as exc:
                return [TextContent(type="text",
                    text=json.dumps({"error": f"Failed to write to OPA: {exc}"}))]

            # Invalidate caches
            with _opa_cache_lock:
                _opa_cache.clear()
                _fields_to_redact_cache.clear()

            mcp_policy_updates_total.labels(section=section).inc()
            await log_access(agent_id, agent_role, "policy", section,
                             "manage_policies.update", "allow",
                             {"old_value": old_value, "new_value": new_data})
            return [TextContent(type="text",
                text=json.dumps({"status": "updated", "section": section},
                                ensure_ascii=False, indent=2))]

        return [TextContent(type="text",
            text=json.dumps({"error": f"Unknown action: {action!r}"}))]

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
            rules = {"error": f"Rules could not be retrieved: {str(e)}"}

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
            resp = await http.post(
                f"{INGESTION_URL}/ingest",
                json={
                    "source": arguments["source"],
                    "source_type": arguments.get("source_type", "text"),
                    "project": arguments.get("project"),
                    "classification": arguments.get("classification", "internal"),
                    "metadata": arguments.get("metadata", {}),
                },
                headers=_ingestion_headers(),
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            result = {"error": f"Ingestion failed: {str(e)}"}

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

        # B-31: redact PII-sensitive metadata fields
        if PII_METADATA_FIELDS:
            purpose = arguments.get("purpose", "")
            for item in reranked:
                if item.get("metadata"):
                    item["metadata"] = await _redact_metadata_pii(
                        item["metadata"], purpose)

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
        return [TextContent(type="text", text=json.dumps({"error": "Resource not found"}))]

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
                data = {"error": f"Unknown graph action: {action}"}
        except Exception as e:
            log.error(f"Graph query failed: {e}")
            data = {"error": str(e)}

        # B-30: mask PII in graph results before returning
        if "error" not in data:
            data = await _mask_graph_pii(data)

        await log_access(agent_id, agent_role, "graph", action, "graph_query", "allow")
        return [TextContent(type="text",
            text=json.dumps(data, ensure_ascii=False, indent=2, default=str))]

    # ── graph_mutate ─────────────────────────────────────────
    elif name == "graph_mutate":
        if agent_role not in ("developer", "admin"):
            await log_access(agent_id, agent_role, "graph", arguments["action"], "graph_mutate", "deny")
            return [TextContent(type="text",
                text=json.dumps({"error": "Graph mutations require developer or admin role"}))]

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
                data = {"error": f"Unknown graph mutation: {action}"}
        except Exception as e:
            log.error(f"Graph mutation failed: {e}")
            data = {"error": str(e)}

        # B-30: mask PII in graph mutation results before returning
        if "error" not in data:
            data = await _mask_graph_pii(data)

        await log_access(agent_id, agent_role, "graph", action, "graph_mutate", "allow")
        return [TextContent(type="text",
            text=json.dumps(data, ensure_ascii=False, indent=2, default=str))]

    # ── submit_feedback (building block 3) ───────────────────
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

    # ── get_eval_stats (building block 3) ────────────────────
    elif name == "get_eval_stats":
        days = arguments.get("days", 30)
        pool = await get_pg_pool()

        # Run the four independent feedback queries in parallel —
        # they all hit the same table and indexes but the round-trip
        # latency was previously summed sequentially.
        stats, worst, trend_current, trend_previous = await asyncio.gather(
            pool.fetchrow("""
                SELECT
                    COUNT(*)                          AS total_feedback,
                    ROUND(AVG(rating)::numeric, 2)    AS avg_rating,
                    ROUND(100.0 * SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1)
                                                      AS satisfaction_pct
                FROM search_feedback
                WHERE created_at > now() - ($1 || ' days')::interval
            """, str(days)),
            pool.fetch("""
                SELECT query, ROUND(AVG(rating)::numeric, 2) AS avg_rating, COUNT(*) AS feedback_count
                FROM search_feedback
                WHERE created_at > now() - ($1 || ' days')::interval
                GROUP BY query
                HAVING COUNT(*) >= 2
                ORDER BY avg_rating ASC
                LIMIT 10
            """, str(days)),
            pool.fetchrow("""
                SELECT ROUND(AVG(rating)::numeric, 2) AS avg_rating
                FROM search_feedback
                WHERE created_at > now() - ($1 || ' days')::interval
            """, str(days)),
            pool.fetchrow("""
                SELECT ROUND(AVG(rating)::numeric, 2) AS avg_rating
                FROM search_feedback
                WHERE created_at BETWEEN now() - ($1 || ' days')::interval * 2
                              AND now() - ($1 || ' days')::interval
            """, str(days)),
        )

        # B-45: windowed metrics + drift baselines. Both queries are
        # independent and the optional UndefinedTableError is caught
        # individually so a missing migration on one side does not
        # block the other.
        async def _fetch_windowed() -> list[dict]:
            try:
                w_rows = await pool.fetch(
                    "SELECT window_label, collection, sample_count, "
                    "       avg_rating, empty_result_rate, avg_rerank_score "
                    "FROM v_feedback_windowed"
                )
            except Exception as e:
                log.debug(f"v_feedback_windowed not readable: {e}")
                return []
            return [{
                "window":     r["window_label"],
                "collection": r["collection"],
                "samples":    int(r["sample_count"] or 0),
                "avg_rating": float(r["avg_rating"]) if r["avg_rating"] is not None else None,
                "empty_rate": float(r["empty_result_rate"]) if r["empty_result_rate"] is not None else None,
                "rerank":     float(r["avg_rerank_score"]) if r["avg_rerank_score"] is not None else None,
            } for r in w_rows]

        async def _fetch_drift_baselines() -> list[dict]:
            try:
                d_rows = await pool.fetch(
                    "SELECT DISTINCT ON (collection) "
                    "       collection, seeded_at, sample_count, embedding_dim "
                    "FROM embedding_reference_set "
                    "ORDER BY collection, seeded_at DESC"
                )
            except Exception as e:
                log.debug(f"embedding_reference_set not readable: {e}")
                return []
            return [{
                "collection":    r["collection"],
                "seeded_at":     r["seeded_at"].isoformat() if r["seeded_at"] else None,
                "sample_count":  int(r["sample_count"]),
                "embedding_dim": int(r["embedding_dim"]),
            } for r in d_rows]

        windowed, drift_status = await asyncio.gather(
            _fetch_windowed(), _fetch_drift_baselines(),
        )

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
            "windowed": windowed,
            "drift_baselines": drift_status,
        }
        return [TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── create_snapshot (building block 4) ───────────────────
    elif name == "create_snapshot":
        if agent_role != "admin":
            return [TextContent(type="text",
                text=json.dumps({"error": "Creating snapshots requires the admin role"}))]

        snapshot_name = arguments["name"]
        description   = arguments.get("description", "")

        try:
            resp = await http.post(
                f"{INGESTION_URL}/snapshots/create",
                json={
                    "name": snapshot_name, "description": description,
                    "created_by": agent_id,
                },
                headers=_ingestion_headers(),
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            result = {"error": f"Snapshot creation failed: {str(e)}"}

        await log_access(agent_id, agent_role, "snapshot", snapshot_name,
                         "create_snapshot", "allow")
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── list_snapshots (building block 4) ────────────────────
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

    # ── delete_documents ────────────────────────────────────
    elif name == "delete_documents":
        confirm = arguments.get("confirm", False)
        if not confirm:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Parameter 'confirm' must be true for delete operations"}))]

        source_type = arguments.get("source_type")
        project = arguments.get("project")
        delete_all = arguments.get("delete_all", False)

        if not delete_all and not source_type and not project:
            return [TextContent(type="text", text=json.dumps(
                {"error": "At least one filter (source_type, project) is required, "
                          "or set delete_all=true"}))]

        # OPA policy check — write action, only admin/developer
        policy = await check_opa_policy(
            agent_id, agent_role, "documents", "internal", "write")
        if not policy["allowed"]:
            await log_access(agent_id, agent_role, "documents", "bulk_delete",
                             "delete", "deny",
                             {"source_type": source_type, "project": project})
            return [TextContent(type="text", text=json.dumps(
                {"error": "Access denied by policy — delete requires write permission"}))]

        qdrant_filter, pg_where, pg_params = _build_delete_filter(
            source_type, project, delete_all)

        pool = await get_pg_pool()
        result: dict[str, Any] = {"deleted": {}, "filters": {
            "source_type": source_type, "project": project, "delete_all": delete_all,
        }, "errors": []}

        # 1. Fetch doc_ids from PostgreSQL (needed for graph cleanup + vault count)
        doc_rows = await pool.fetch(
            f"SELECT id FROM documents_meta WHERE {pg_where}", *pg_params)
        doc_ids = [str(r["id"]) for r in doc_rows]
        result["deleted"]["documents_meta"] = len(doc_ids)

        # 2. Count vault entries before CASCADE deletes them
        vault_count = 0
        if doc_ids:
            vault_count = await pool.fetchval(
                "SELECT count(*) FROM pii_vault.original_content "
                "WHERE document_id = ANY($1::uuid[])", doc_ids) or 0
        result["deleted"]["vault_entries"] = vault_count

        # 3. Delete from Qdrant (all collections)
        collections = ["pb_general", "pb_code", "pb_rules"]
        qdrant_counts: dict[str, int] = {}
        for col in collections:
            try:
                count_result = await qdrant.count(
                    collection_name=col,
                    count_filter=qdrant_filter,
                    exact=True,
                )
                qdrant_counts[col] = count_result.count
                if count_result.count > 0:
                    await qdrant.delete(
                        collection_name=col,
                        points_selector=FilterSelector(filter=qdrant_filter)
                            if qdrant_filter else FilterSelector(
                                filter=Filter(must=[])),
                    )
            except Exception as e:
                log.error(f"Qdrant delete failed for {col}: {e}")
                qdrant_counts[col] = 0
                result["errors"].append(f"qdrant/{col}: {str(e)}")
        result["deleted"]["qdrant"] = qdrant_counts

        # 4. Delete from PostgreSQL (CASCADE handles vault)
        if doc_ids:
            await pool.execute(
                f"DELETE FROM documents_meta WHERE {pg_where}", *pg_params)

        # 5. Delete Document nodes from Knowledge Graph
        graph_deleted = 0
        for doc_id in doc_ids:
            try:
                await graph.delete_node(pool, "Document", doc_id)
                graph_deleted += 1
            except Exception:
                pass  # Node may not exist in graph
        result["deleted"]["graph_nodes"] = graph_deleted

        # 6. Append a redaction event to the audit log.
        #    The log is append-only (hash-chain, EU AI Act Art. 12) — we cannot
        #    mutate historic rows. log_access() already PII-scans context at
        #    write-time, so prior entries contain masked queries; this row
        #    documents the Art. 17 erasure action itself.
        if doc_ids:
            await log_access(agent_id, agent_role, "audit", "redaction",
                             "pii_redaction_event", "allow",
                             {"reason": "bulk_delete",
                              "affected_resource_ids": doc_ids,
                              "count": len(doc_ids)})

        if not result["errors"]:
            del result["errors"]

        await log_access(agent_id, agent_role, "documents", "bulk_delete",
                         "delete", "allow",
                         {"source_type": source_type, "project": project,
                          "delete_all": delete_all, "count": len(doc_ids)})
        return [TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── EU AI Act Art. 14: review_pending ────────────────────
    elif name == "review_pending":
        if agent_role != "admin":
            await log_access(agent_id, agent_role, "oversight", "review",
                             "review_pending", "deny")
            return [TextContent(type="text", text=json.dumps(
                {"error": "review_pending requires admin role"}))]

        action    = arguments.get("action", "list")
        pool      = await get_pg_pool()

        if action == "list":
            limit = int(arguments.get("limit", 50))
            limit = max(1, min(500, limit))
            rows = await pool.fetch(
                "SELECT id::text, agent_id, agent_role, tool, arguments, "
                "       classification, status, reason, created_at, expires_at "
                "FROM pending_reviews "
                "WHERE status = 'pending' "
                "ORDER BY created_at ASC "
                f"LIMIT {limit}"
            )
            out = [{
                "review_id":      r["id"],
                "agent_id":       r["agent_id"],
                "agent_role":     r["agent_role"],
                "tool":           r["tool"],
                "arguments":      (r["arguments"] if isinstance(r["arguments"], dict)
                                   else json.loads(r["arguments"] or "{}")),
                "classification": r["classification"],
                "status":         r["status"],
                "reason":         r["reason"],
                "created_at":     r["created_at"].isoformat() if r["created_at"] else None,
                "expires_at":     r["expires_at"].isoformat() if r["expires_at"] else None,
            } for r in rows]
            await log_access(agent_id, agent_role, "oversight", "review",
                             "review_pending.list", "allow",
                             {"count": len(out)})
            return [TextContent(type="text", text=json.dumps(
                {"pending": out, "count": len(out)},
                ensure_ascii=False, indent=2,
            ))]

        if action in ("approve", "deny"):
            review_id = arguments.get("review_id")
            if not review_id:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "review_id is required for approve/deny"}))]
            new_status = "approved" if action == "approve" else "denied"
            row = await pool.fetchrow(
                "UPDATE pending_reviews SET status = $1, "
                "decision_by = $2, decision_at = now(), reason = $3 "
                "WHERE id = $4::uuid AND status = 'pending' "
                "RETURNING id::text, status, tool, agent_id, classification",
                new_status, agent_id,
                arguments.get("reason") or "",
                review_id,
            )
            if row is None:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "review not found or already decided",
                     "review_id": review_id}))]
            await log_access(agent_id, agent_role, "oversight", "review",
                             f"review_pending.{action}", "allow",
                             {"review_id": review_id, "status": new_status})
            return [TextContent(type="text", text=json.dumps({
                "review_id": row["id"],
                "status":    row["status"],
                "tool":      row["tool"],
                "agent_id":  row["agent_id"],
                "classification": row["classification"],
            }, ensure_ascii=False, indent=2))]

        return [TextContent(type="text", text=json.dumps(
            {"error": f"unknown action: {action}"}))]

    # ── EU AI Act Art. 14: get_review_status ─────────────────
    elif name == "get_review_status":
        review_id = arguments.get("review_id")
        if not review_id:
            return [TextContent(type="text", text=json.dumps(
                {"error": "review_id is required"}))]

        pool = await get_pg_pool()
        row = await pool.fetchrow(
            "SELECT id::text, agent_id, agent_role, tool, arguments, "
            "       classification, status, reason, decision_by, decision_at, "
            "       created_at, expires_at "
            "FROM pending_reviews WHERE id = $1::uuid",
            review_id,
        )
        if row is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "review not found", "review_id": review_id}))]

        # Agents may only read their own reviews; admins see everything.
        if agent_role != "admin" and row["agent_id"] != agent_id:
            await log_access(agent_id, agent_role, "oversight", "review",
                             "get_review_status", "deny",
                             {"review_id": review_id,
                              "reason": "owner_mismatch"})
            return [TextContent(type="text", text=json.dumps(
                {"error": "not your review"}))]

        payload = {
            "review_id":      row["id"],
            "status":         row["status"],
            "tool":           row["tool"],
            "classification": row["classification"],
            "reason":         row["reason"],
            "decision_by":    row["decision_by"],
            "decision_at":    row["decision_at"].isoformat() if row["decision_at"] else None,
            "created_at":     row["created_at"].isoformat() if row["created_at"] else None,
            "expires_at":     row["expires_at"].isoformat() if row["expires_at"] else None,
        }
        return [TextContent(type="text", text=json.dumps(
            payload, ensure_ascii=False, indent=2))]

    # ── EU AI Act Art. 11: generate_compliance_doc ────────────
    elif name == "generate_compliance_doc":
        if agent_role != "admin":
            await log_access(agent_id, agent_role, "compliance", "generate",
                             "generate_compliance_doc", "deny")
            return [TextContent(type="text", text=json.dumps(
                {"error": "generate_compliance_doc requires admin role"}))]

        from compliance_doc import generate_annex_iv_doc

        async def _eval_loader() -> dict:
            # Re-uses the same code path as the get_eval_stats tool by
            # invoking _dispatch internally — keeps the renderer aligned
            # with what an admin would see in get_eval_stats.
            res = await _dispatch("get_eval_stats", {"days": 30},
                                  agent_id, agent_role)
            try:
                return json.loads(res[0].text)
            except Exception:
                return {}

        try:
            output_mode = arguments.get("output_mode", "inline")
            result = await generate_annex_iv_doc(
                transparency_loader=_get_transparency_report,
                health_loader=_build_risk_health_payload,
                eval_stats_loader=_eval_loader,
                output_mode=output_mode,
            )
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps(
                {"error": str(e)}))]

        await log_access(agent_id, agent_role, "compliance", "generate",
                         "generate_compliance_doc", "allow",
                         {"output_mode": result.get("output_mode"),
                          "size_bytes": result.get("size_bytes"),
                          "report_version": result.get("report_version")})
        return [TextContent(type="text", text=json.dumps(
            result, ensure_ascii=False, indent=2))]

    # ── EU AI Act Art. 13: get_system_info ───────────────────
    elif name == "get_system_info":
        payload = await _get_transparency_report()
        await log_access(agent_id, agent_role, "transparency", "report",
                         "get_system_info", "allow",
                         {"report_version": payload.get("report_version")})
        return [TextContent(type="text",
            text=json.dumps(payload, ensure_ascii=False, indent=2))]

    # ── EU AI Act Art. 12: verify_audit_integrity ─────────────
    elif name == "verify_audit_integrity":
        if agent_role != "admin":
            await log_access(agent_id, agent_role, "audit", "verify",
                             "verify_audit_integrity", "deny")
            return [TextContent(type="text", text=json.dumps(
                {"error": "verify_audit_integrity requires admin role"}))]

        start_id = arguments.get("start_id")
        end_id   = arguments.get("end_id")

        pool = await get_pg_pool()
        row = await pool.fetchrow(
            "SELECT valid, first_invalid_id, total_checked, "
            "       encode(last_valid_hash, 'hex') AS last_valid_hash "
            "FROM pb_verify_audit_chain($1, $2)",
            start_id, end_id,
        )
        result = {
            "valid":            row["valid"],
            "first_invalid_id": row["first_invalid_id"],
            "total_checked":    row["total_checked"],
            "last_valid_hash":  row["last_valid_hash"],
            "range": {"start_id": start_id, "end_id": end_id},
        }
        await log_access(agent_id, agent_role, "audit", "verify",
                         "verify_audit_integrity", "allow",
                         {"valid": row["valid"], "total_checked": row["total_checked"]})
        return [TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── EU AI Act Art. 12: export_audit_log ───────────────────
    elif name == "export_audit_log":
        if agent_role != "admin":
            await log_access(agent_id, agent_role, "audit", "export",
                             "export_audit_log", "deny")
            return [TextContent(type="text", text=json.dumps(
                {"error": "export_audit_log requires admin role"}))]

        # Read caps from OPA data
        try:
            cfg_resp = await http.get(f"{OPA_URL}/v1/data/pb/config/audit")
            cfg_resp.raise_for_status()
            audit_cfg = cfg_resp.json().get("result", {}) or {}
        except Exception as e:
            log.warning(f"Could not load audit config from OPA, using defaults: {e}")
            audit_cfg = {}
        export_max_rows     = int(audit_cfg.get("export_max_rows",     100000))
        export_default_rows = int(audit_cfg.get("export_default_rows", 10000))
        export_formats      = audit_cfg.get("export_formats", ["json", "csv"])

        fmt = arguments.get("format", "json")
        if fmt not in export_formats:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"format '{fmt}' not allowed; allowed: {export_formats}"}))]

        requested_limit = int(arguments.get("limit") or export_default_rows)
        limit = min(max(1, requested_limit), export_max_rows)

        # Parse + bind ISO-8601 datetimes to real datetime objects so
        # asyncpg's TIMESTAMPTZ type-check accepts them (#96).
        try:
            since_dt = _parse_iso_datetime(arguments.get("since"), field="since")
            until_dt = _parse_iso_datetime(arguments.get("until"), field="until")
        except ValueError as e:
            return [TextContent(type="text",
                text=json.dumps({"error": str(e)}, ensure_ascii=False))]

        # Build filter
        where_parts: list[str] = []
        params: list = []
        idx = 1
        if since_dt is not None:
            where_parts.append(f"created_at >= ${idx}")
            params.append(since_dt)
            idx += 1
        if until_dt is not None:
            where_parts.append(f"created_at < ${idx}")
            params.append(until_dt)
            idx += 1
        if arguments.get("agent_id"):
            where_parts.append(f"agent_id = ${idx}")
            params.append(arguments["agent_id"])
            idx += 1
        if arguments.get("action"):
            where_parts.append(f"action = ${idx}")
            params.append(arguments["action"])
            idx += 1
        where_sql = " AND ".join(where_parts) if where_parts else "TRUE"

        pool = await get_pg_pool()
        rows = await pool.fetch(
            f"SELECT id, agent_id, agent_role, resource_type, resource_id, "
            f"       action, policy_result, policy_reason, "
            f"       contains_pii, purpose, legal_basis, data_category, "
            f"       fields_redacted, created_at, "
            f"       encode(prev_hash, 'hex')  AS prev_hash, "
            f"       encode(entry_hash, 'hex') AS entry_hash "
            f"FROM agent_access_log "
            f"WHERE {where_sql} "
            f"ORDER BY id ASC "
            f"LIMIT {limit}",
            *params,
        )

        def _row_to_dict(r) -> dict:
            return {
                "id":              r["id"],
                "agent_id":        r["agent_id"],
                "agent_role":      r["agent_role"],
                "resource_type":   r["resource_type"],
                "resource_id":     r["resource_id"],
                "action":          r["action"],
                "policy_result":   r["policy_result"],
                "policy_reason":   r["policy_reason"],
                "contains_pii":    r["contains_pii"],
                "purpose":         r["purpose"],
                "legal_basis":     r["legal_basis"],
                "data_category":   r["data_category"],
                "fields_redacted": r["fields_redacted"],
                "created_at":      r["created_at"].isoformat() if r["created_at"] else None,
                "prev_hash":       r["prev_hash"],
                "entry_hash":      r["entry_hash"],
            }

        if fmt == "json":
            payload = {
                "format":  "json",
                "count":   len(rows),
                "limit":   limit,
                "entries": [_row_to_dict(r) for r in rows],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2)
        else:  # csv
            import io, csv
            buf = io.StringIO()
            fieldnames = [
                "id", "agent_id", "agent_role", "resource_type", "resource_id",
                "action", "policy_result", "policy_reason", "contains_pii",
                "purpose", "legal_basis", "data_category", "fields_redacted",
                "created_at", "prev_hash", "entry_hash",
            ]
            writer = csv.DictWriter(buf, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                d = _row_to_dict(r)
                d["fields_redacted"] = ";".join(d["fields_redacted"] or [])
                writer.writerow(d)
            body = buf.getvalue()

        await log_access(agent_id, agent_role, "audit", "export",
                         "export_audit_log", "allow",
                         {"format": fmt, "count": len(rows), "limit": limit})
        return [TextContent(type="text", text=body)]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ── B-44: Risk-indicator health payload ──────────────────────
# Severity ordering used when combining indicators into a single status.
_RISK_SEVERITY_ORDER = ["ok", "info", "warning", "medium", "high", "critical"]


def _worst_severity(a: str, b: str) -> str:
    """Return the more severe of two severities."""
    ai = _RISK_SEVERITY_ORDER.index(a) if a in _RISK_SEVERITY_ORDER else 0
    bi = _RISK_SEVERITY_ORDER.index(b) if b in _RISK_SEVERITY_ORDER else 0
    return _RISK_SEVERITY_ORDER[max(ai, bi)]


async def _check_opa_reachable() -> dict:
    """R-05: OPA Policy Engine reachability."""
    try:
        resp = await http.get(f"{OPA_URL}/health", timeout=2.0)
        resp.raise_for_status()
        return {"name": "opa_reachable", "value": True, "severity": "ok",
                "risk": "R-05"}
    except Exception as e:
        return {"name": "opa_reachable", "value": False, "severity": "critical",
                "risk": "R-05", "detail": str(e)[:200]}


async def _check_pii_scanner() -> dict:
    """R-02, R-07: PII Scanner availability via ingestion /health."""
    try:
        resp = await http.get(f"{INGESTION_URL}/health", timeout=2.0)
        resp.raise_for_status()
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        enabled = body.get("pii_scanner_enabled", True)  # default to enabled if unknown
        if not enabled:
            return {"name": "pii_scanner_status", "value": "disabled",
                    "severity": "high", "risk": "R-02"}
        return {"name": "pii_scanner_status", "value": "enabled",
                "severity": "ok", "risk": "R-02"}
    except Exception as e:
        return {"name": "pii_scanner_status", "value": "unreachable",
                "severity": "high", "risk": "R-02", "detail": str(e)[:200]}


async def _check_reranker_available() -> dict:
    """R-04: Reranker availability (medium — graceful fallback exists)."""
    if not RERANKER_ENABLED:
        return {"name": "reranker_available", "value": "disabled",
                "severity": "info", "risk": "R-04"}
    try:
        resp = await http.get(f"{RERANKER_URL}/health", timeout=2.0)
        resp.raise_for_status()
        return {"name": "reranker_available", "value": True, "severity": "ok",
                "risk": "R-04"}
    except Exception as e:
        return {"name": "reranker_available", "value": False,
                "severity": "medium", "risk": "R-04", "detail": str(e)[:200]}


HEALTH_CHAIN_TAIL_ROWS = int(os.getenv("HEALTH_CHAIN_TAIL_ROWS", "1000"))


async def _check_audit_chain() -> dict:
    """R-03: Audit-log hash chain integrity.

    Uses pb_verify_audit_chain_tail() so a JSON health request stays
    cheap even when agent_access_log has millions of rows. The full
    chain is verified by the daily audit_retention_cleanup job.
    """
    try:
        pool = await get_pg_pool()
        row = await pool.fetchrow(
            "SELECT valid, first_invalid_id, total_checked "
            "FROM pb_verify_audit_chain_tail($1)",
            HEALTH_CHAIN_TAIL_ROWS,
        )
        if row is None:
            return {"name": "audit_chain_integrity", "value": "unknown",
                    "severity": "warning", "risk": "R-03"}
        if row["valid"]:
            return {"name": "audit_chain_integrity", "value": "valid",
                    "severity": "ok", "risk": "R-03",
                    "total_checked": row["total_checked"]}
        return {"name": "audit_chain_integrity", "value": "invalid",
                "severity": "critical", "risk": "R-03",
                "first_invalid_id": row["first_invalid_id"],
                "total_checked": row["total_checked"]}
    except Exception as e:
        return {"name": "audit_chain_integrity", "value": "error",
                "severity": "warning", "risk": "R-03", "detail": str(e)[:200]}


async def _check_feedback_score() -> dict:
    """R-01, R-04: Recent feedback average."""
    try:
        pool = await get_pg_pool()
        avg = await pool.fetchval(
            "SELECT AVG(rating)::float FROM search_feedback "
            "WHERE created_at > now() - INTERVAL '24 hours'"
        )
        if avg is None:
            return {"name": "feedback_score", "value": None, "severity": "ok",
                    "risk": "R-01", "detail": "no feedback in last 24h"}
        sev = "warning" if avg < 2.5 else "ok"
        return {"name": "feedback_score", "value": round(float(avg), 2),
                "severity": sev, "risk": "R-01"}
    except Exception as e:
        return {"name": "feedback_score", "value": None, "severity": "info",
                "risk": "R-01", "detail": str(e)[:200]}


async def _check_circuit_breaker_indicator() -> dict:
    """R-07: Human-oversight kill-switch state.

    Active breaker is a *critical* indicator: it means data retrieval
    is intentionally suspended and the deployer should know.
    """
    try:
        state = await _get_circuit_breaker_state()
        if state.get("active"):
            return {
                "name": "circuit_breaker_active", "value": True,
                "severity": "critical", "risk": "R-07",
                "reason": state.get("reason"),
                "set_by": state.get("set_by"),
                "set_at": state.get("set_at"),
            }
        return {"name": "circuit_breaker_active", "value": False,
                "severity": "info", "risk": "R-07"}
    except Exception as e:
        return {"name": "circuit_breaker_active", "value": None,
                "severity": "warning", "risk": "R-07",
                "detail": str(e)[:200]}


async def _build_risk_health_payload() -> dict:
    """Assemble all risk indicators into a single JSON document."""
    indicators = await asyncio.gather(
        _check_opa_reachable(),
        _check_pii_scanner(),
        _check_reranker_available(),
        _check_audit_chain(),
        _check_feedback_score(),
        _check_circuit_breaker_indicator(),
        return_exceptions=True,
    )

    safe_indicators: list[dict] = []
    for ind in indicators:
        if isinstance(ind, Exception):
            safe_indicators.append({
                "name": "unknown", "value": None, "severity": "warning",
                "detail": str(ind)[:200],
            })
        else:
            safe_indicators.append(ind)

    # Combine severities — "ok" if everything is ok, else worst.
    combined = "ok"
    for ind in safe_indicators:
        combined = _worst_severity(combined, ind.get("severity", "ok"))

    return {
        "service": "mcp-server",
        "edition": "community",
        "status": combined,
        "indicators": safe_indicators,
        "risk_register": "docs/risk-management.md",
    }


# ── B-41: Transparency report (EU AI Act Art. 13) ────────────
# Deployer-facing information about the system: models, policies,
# collection stats, PII configuration, audit integrity.
# Auth required (any valid pb_ key) — not public to the internet.
_TRANSPARENCY_CACHE: dict[str, Any] = {
    "payload": None,
    "version": None,
    "built_at": 0.0,
}
TRANSPARENCY_CACHE_TTL = float(os.getenv("TRANSPARENCY_CACHE_TTL", "60"))


def _compute_transparency_version(models: dict, opa_hash: str,
                                  collections: list) -> str:
    """Deterministic fingerprint used as cache key and report_version."""
    import hashlib
    canonical = json.dumps({
        "models": models,
        "opa": opa_hash,
        "collections": sorted(collections),
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


async def _transparency_opa_snapshot() -> tuple[dict, str]:
    """Fetch the public portion of OPA config and return (snapshot, hash).

    Only exposes non-sensitive meta-info: role list, classification list,
    pii_entity_types, summarization policy flags, audit retention. Never
    exposes secrets or the full access matrix.
    """
    import hashlib
    try:
        resp = await http.get(f"{OPA_URL}/v1/data/pb/config", timeout=2.0)
        resp.raise_for_status()
        cfg = resp.json().get("result", {}) or {}
    except Exception as e:
        log.warning(f"Transparency: OPA config fetch failed: {e}")
        cfg = {}

    snapshot = {
        "roles":                    cfg.get("roles", []),
        "classifications":          cfg.get("classifications", []),
        "pii_entity_types":         cfg.get("pii_entity_types", []),
        "audit_retention_days":     (cfg.get("audit") or {}).get("retention_days"),
        "summarization":            cfg.get("summarization", {}),
        "proxy_allowed_roles":      (cfg.get("proxy") or {}).get("allowed_roles", []),
        "active_policies": [
            "pb.access", "pb.privacy", "pb.summarization",
            "pb.proxy", "pb.rules",
        ],
    }
    opa_hash = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True).encode()
    ).hexdigest()[:16]
    return snapshot, opa_hash


async def _transparency_qdrant_snapshot() -> list[dict]:
    """Return basic per-collection stats (name, vectors_count) for the
    three canonical Powerbrain collections. Collections are queried in
    parallel via asyncio.gather."""
    names = ("pb_general", "pb_code", "pb_rules")

    async def _one(name: str) -> dict:
        try:
            info = await qdrant.get_collection(collection_name=name)
            return {
                "name":    name,
                "status":  getattr(info, "status", None) and str(info.status),
                "points":  getattr(info, "points_count", None),
                "vectors": getattr(info, "vectors_count", None),
            }
        except Exception as e:
            return {"name": name, "status": "unavailable",
                    "detail": str(e)[:200]}

    return list(await asyncio.gather(*[_one(n) for n in names]))


async def _transparency_pii_snapshot() -> dict:
    """Fetch PII scanner config from ingestion /health."""
    try:
        resp = await http.get(f"{INGESTION_URL}/health", timeout=2.0)
        resp.raise_for_status()
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return {
            "scanner_enabled": body.get("pii_scanner_enabled", None),
            "languages":       body.get("pii_languages"),
            "entity_types":    body.get("pii_entity_types"),
        }
    except Exception as e:
        return {"scanner_enabled": None, "detail": str(e)[:200]}


async def _transparency_audit_snapshot() -> dict:
    """Audit-chain integrity from the worker-maintained cache.

    Reads `audit_integrity_status` (single-row table refreshed by the
    `audit_integrity_status_refresh` worker job, ~ every 60 s by
    default). Decoupling the snapshot from the request path means the
    reported state is always *committed* and not affected by the
    request's own audit-log INSERT (issue #95). Consumers see
    `checked_at` and can decide for themselves how stale is too stale;
    a live answer is available through the `verify_audit_integrity`
    MCP tool.
    """
    try:
        pool = await get_pg_pool()
        row = await pool.fetchrow(
            "SELECT valid, total_checked, first_invalid_id, "
            "       checked_at, error "
            "  FROM audit_integrity_status WHERE id = 1"
        )
        if row is None:
            # `total_checked: null` (not 0) so consumers can distinguish
            # "no check has run" from "check ran, found 0 entries". The
            # `stale` flag is the primary signal; the null value avoids
            # the misleading "verified: 0" rendering downstream (#104).
            return {
                "valid":         None,
                "total_checked": None,
                "stale":         True,
                "detail":        "no integrity check has run yet",
            }
        if row["error"]:
            return {
                "valid":      None,
                "detail":     row["error"][:200],
                "checked_at": row["checked_at"].isoformat() if row["checked_at"] else None,
            }
        return {
            "valid":            row["valid"],
            "total_checked":    row["total_checked"],
            "first_invalid_id": row["first_invalid_id"],
            "checked_at":       row["checked_at"].isoformat() if row["checked_at"] else None,
        }
    except Exception as e:
        return {"valid": None, "detail": str(e)[:200]}


async def _build_transparency_payload() -> dict:
    """Build the full transparency report (no cache).

    The four snapshot helpers are independent — fan them out via
    asyncio.gather so a cold cache miss takes ~max(opa, qdrant, ing,
    pg) instead of the sum.
    """
    (opa_snapshot, opa_hash), collections, pii, audit = await asyncio.gather(
        _transparency_opa_snapshot(),
        _transparency_qdrant_snapshot(),
        _transparency_pii_snapshot(),
        _transparency_audit_snapshot(),
    )

    summarization_split = (
        SUMMARIZATION_PROVIDER_URL != LLM_PROVIDER_URL
        or SUMMARIZATION_MODEL != LLM_MODEL
    )
    models = {
        "embedding": {
            "name":        EMBEDDING_MODEL,
            "provider_url": EMBEDDING_PROVIDER_URL,
        },
        "llm": {
            # mcp-server consumes an LLM only for summarization. Field
            # name kept as "llm" for back-compat with consumers like
            # compliance_doc.py; the values reflect SUMMARIZATION_*.
            "name":        SUMMARIZATION_MODEL,
            "provider_url": SUMMARIZATION_PROVIDER_URL,
            "purpose":     "summarization",
            "pool_split":  summarization_split,
        },
        "reranker": {
            "backend":     RERANKER_BACKEND,
            "model":       RERANKER_MODEL_NAME or None,
            "enabled":     RERANKER_ENABLED,
        },
    }

    version = _compute_transparency_version(
        models, opa_hash, [c["name"] for c in collections]
    )

    return {
        "service":        "mcp-server",
        "edition":        "community",
        "report_version": version,
        "system_purpose": (
            "Powerbrain feeds AI agents with policy-compliant enterprise "
            "knowledge via the Model Context Protocol. It is not itself a "
            "high-risk AI system — see docs/risk-management.md for the "
            "full risk register."
        ),
        "deployment_constraints": [
            "All data access is mediated by OPA policies",
            "PII scanning runs at ingestion and on the chat path",
            "Audit log is append-only with a SHA-256 hash chain",
            "Summarization of confidential data is mandatory",
        ],
        "models":           models,
        "opa":              opa_snapshot,
        "collections":      collections,
        "pii_scanner":      pii,
        "audit_integrity":  audit,
        "risk_register":    "docs/risk-management.md",
    }


def _invalidate_transparency_cache() -> None:
    """Force the next /transparency hit to rebuild the report.

    Call after admin-triggered config changes (model swap, OPA bundle
    reload, collection drop) so deployers see fresh state immediately
    instead of waiting up to TRANSPARENCY_CACHE_TTL seconds.
    """
    _TRANSPARENCY_CACHE["payload"]  = None
    _TRANSPARENCY_CACHE["version"]  = None
    _TRANSPARENCY_CACHE["built_at"] = 0.0


async def _get_transparency_report() -> dict:
    """Return a cached transparency report (60s TTL, or invalidated by
    version fingerprint changes)."""
    import time as _time
    now = _time.time()
    cached = _TRANSPARENCY_CACHE.get("payload")
    built  = _TRANSPARENCY_CACHE.get("built_at", 0.0)

    if cached is not None and (now - built) < TRANSPARENCY_CACHE_TTL:
        return cached

    payload = await _build_transparency_payload()
    _TRANSPARENCY_CACHE["payload"]  = payload
    _TRANSPARENCY_CACHE["version"]  = payload["report_version"]
    _TRANSPARENCY_CACHE["built_at"] = now
    return payload


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

        # Refuse to start if OPA is unreachable or a required policy
        # package is not loaded (issue #59 part 2).
        if os.getenv("SKIP_OPA_STARTUP_CHECK", "false").lower() != "true":
            await verify_required_policies(
                http, OPA_URL,
                [
                    "pb/access/allow",
                    "pb/summarization/summarize_allowed",
                    "pb/privacy/vault_access_allowed",
                ],
            )

        oauth_provider.start_cleanup()
        async with session_manager.run():
            yield

        # ── Shutdown ──
        await http.aclose()
        await pg_pool.close()
        log.info("Shutdown: PG pool and HTTP client closed")

    def _auth_user(request):
        """Return the authenticated user from Starlette request.state, if any."""
        try:
            user = request.scope.get("user") or getattr(request.state, "user", None)
            if user is None:
                return None, None
            # MCP SDK puts an AuthenticatedUser on scope; adapt defensively.
            client_id = getattr(user, "username", None) or getattr(user, "client_id", None)
            scopes = getattr(user, "scopes", None) or []
            role = scopes[0] if scopes else None
            return client_id, role
        except Exception:
            return None, None

    async def circuit_breaker_get(request):
        """GET /circuit-breaker — report current kill-switch state.

        Auth-required (any valid pb_ key). Does NOT require admin role so
        operators can monitor the breaker without elevated credentials.
        """
        state = await _get_circuit_breaker_state()
        return JSONResponse(state)

    async def circuit_breaker_post(request):
        """POST /circuit-breaker — flip the kill-switch. Admin only.

        Body: {"active": bool, "reason": str}
        """
        _, role = _auth_user(request)
        if role != "admin":
            return JSONResponse(
                {"error": "circuit_breaker toggle requires admin role"},
                status_code=403,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if "active" not in body:
            return JSONResponse(
                {"error": "body must contain 'active' (bool)"},
                status_code=400,
            )
        agent_id, _ = _auth_user(request)
        state = await _set_circuit_breaker(
            bool(body["active"]),
            body.get("reason"),
            agent_id or "unknown",
        )
        await log_access(agent_id or "unknown", "admin", "oversight",
                         "circuit_breaker", "set_circuit_breaker", "allow",
                         {"active": state["active"], "reason": state.get("reason")})
        return JSONResponse(state)

    async def transparency_endpoint(request):
        """GET /transparency — EU AI Act Art. 13 transparency report.

        Auth-required (any valid pb_ key). Not in AUTH_BYPASS_PATHS — the
        outer RequireAuthMiddleware enforces the Bearer token.
        """
        payload = await _get_transparency_report()
        return JSONResponse(payload)

    async def vault_resolve_endpoint(request):
        """POST /vault/resolve — text-level pseudonym resolution.

        Primary caller is pb-proxy's agent loop: after a tool response
        comes back containing ``[ENTITY_TYPE:hash]`` pseudonyms (because
        the knowledge base stores pseudonyms, not originals), the proxy
        asks this endpoint to replace them with vault-resolved originals
        before handing the text to the LLM. Same OPA policy, same audit
        trail as search_knowledge's inline vault lookup — just over plain
        text instead of per-chunk metadata.

        Body: ``{"text": str, "purpose": str}``.
        Auth: any valid ``pb_`` key. OPA still gates per (role, purpose,
        classification, data_category) so this doesn't widen the vault
        surface.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        text = body.get("text")
        purpose = body.get("purpose")
        if not isinstance(text, str) or not purpose:
            return JSONResponse(
                {"error": "body must contain 'text' (str) and 'purpose' (str)"},
                status_code=400,
            )

        agent_id, role = _auth_user(request)
        if not role:
            return JSONResponse(
                {"error": "authentication required"},
                status_code=401,
            )

        # Use the authenticated-agent's identifier as the token_hash for
        # auditing; these calls are not vault-token-bound but signed via
        # the API key the caller already presented, so we record that
        # association instead of a detached HMAC token fingerprint.
        token_hash = hashlib.sha256(
            f"vault-resolve:{agent_id}".encode()
        ).hexdigest()[:16]

        try:
            result = await vault_resolve_pseudonyms(
                text,
                purpose=purpose,
                agent_role=role,
                agent_id=agent_id or "unknown",
                token_hash=token_hash,
            )
        except Exception as exc:
            log.warning("vault_resolve_pseudonyms failed: %s", exc)
            return JSONResponse(
                {"error": f"resolution failed: {exc}"},
                status_code=500,
            )

        return JSONResponse(result)

    async def health_check(request):
        """Content-negotiated health endpoint.

        - Default: Plain-text "ok" (stable contract for Docker/LB health checks).
        - With ``Accept: application/json``: structured risk-indicator JSON
          covering the EU AI Act Art. 9 risk register. See
          ``docs/risk-management.md`` for the full mapping.
        """
        accept = (request.headers.get("accept") or "").lower()
        wants_json = "application/json" in accept
        if not wants_json:
            return PlainTextResponse("ok")

        payload = await _build_risk_health_payload()
        status_code = 200 if payload["status"] != "critical" else 503
        return JSONResponse(payload, status_code=status_code)

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

    # ── OAuth Provider ──
    public_base = MCP_PUBLIC_URL.rstrip("/")
    issuer_url = f"{public_base}/oauth"  # /oauth suffix for Caddy .well-known routing
    api_key_verifier = ApiKeyVerifier()

    oauth_provider = PowerbrainOAuthProvider(
        api_key_verifier=api_key_verifier,
        login_url=f"{public_base}/authorize/login",
        callback_url=f"{public_base}/authorize/callback",
        get_pool=get_pg_pool,
    )

    # ── OAuth + Login Routes ──
    from pydantic import AnyHttpUrl

    oauth_metadata = {
        "issuer": issuer_url,
        "authorization_endpoint": f"{public_base}/authorize",
        "token_endpoint": f"{public_base}/token",
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "scopes_supported": ["mcp:tools"],
        "registration_endpoint": f"{public_base}/register",
    }
    protected_resource_metadata = {
        "resource": f"{public_base}/mcp",
        "authorization_servers": [issuer_url],
        "scopes_supported": ["mcp:tools"],
    }

    async def well_known_oauth_as(request: Request):
        return JSONResponse(oauth_metadata)

    async def well_known_oauth_pr(request: Request):
        return JSONResponse(protected_resource_metadata)

    async def login_form(request: Request):
        session_id = request.query_params.get("session", "")
        return HTMLResponse(render_login_page(
            callback_url=f"{public_base}/authorize/callback",
            login_session_id=session_id,
        ))

    async def login_callback(request: Request):
        form = await request.form()
        session_id = str(form.get("login_session_id", ""))
        api_key = str(form.get("api_key", ""))

        redirect_url, error = await oauth_provider.handle_login_callback(session_id, api_key)
        if error:
            return HTMLResponse(render_login_page(
                callback_url=f"{public_base}/authorize/callback",
                login_session_id=session_id,
                error=error,
            ), status_code=400)
        return RedirectResponse(url=redirect_url, status_code=302)

    # SDK OAuth routes (authorize, token, register)
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    auth_routes = create_auth_routes(
        provider=oauth_provider,
        issuer_url=AnyHttpUrl(issuer_url),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )

    app = Starlette(
        routes=[
            Route("/health", endpoint=health_check),
            Route("/metrics/json", endpoint=metrics_json),
            Route("/transparency", endpoint=transparency_endpoint),
            Route("/vault/resolve", endpoint=vault_resolve_endpoint, methods=["POST"]),
            Route("/circuit-breaker", endpoint=circuit_breaker_get, methods=["GET"]),
            Route("/circuit-breaker", endpoint=circuit_breaker_post, methods=["POST"]),
            # OAuth discovery (at stripped paths AND full RFC 9728 paths)
            Route("/.well-known/oauth-authorization-server", endpoint=well_known_oauth_as),
            Route("/.well-known/oauth-authorization-server/{rest:path}", endpoint=well_known_oauth_as),
            Route("/.well-known/oauth-protected-resource", endpoint=well_known_oauth_pr),
            Route("/.well-known/oauth-protected-resource/{rest:path}", endpoint=well_known_oauth_pr),
            # Login form + callback (before auth routes to avoid /authorize prefix match)
            Route("/authorize/login", endpoint=login_form, methods=["GET"]),
            Route("/authorize/callback", endpoint=login_callback, methods=["POST"]),
            # SDK auth routes
            *auth_routes,
            # MCP endpoint
            Route(MCP_PATH, endpoint=MCPTransport()),
        ],
        lifespan=lifespan,
    )

    # ── Auth-Middleware (inside-out: last applied = outermost) ──
    starlette_app = app  # keep reference for lifespan bypass
    # Combined verifier: API key first, then OAuth token
    verifier = CombinedTokenVerifier(api_key_verifier, oauth_provider)
    # AuthContextMiddleware: stores authenticated user in contextvars
    app = AuthContextMiddleware(app)
    # RateLimitMiddleware: per-agent token bucket rate limiting (reads scope["user"])
    app = RateLimitMiddleware(app)
    if AUTH_REQUIRED:
        # RequireAuthMiddleware: rejects unauthenticated requests with 401
        # resource_metadata_url: use /powerbrain/ prefix path which routes
        # through Caddy's handle_path reliably (RFC 9728 paths don't work)
        _resource_meta = f"{public_base}/.well-known/oauth-protected-resource"
        app = RequireAuthMiddleware(
            app, required_scopes=[],
            resource_metadata_url=AnyHttpUrl(_resource_meta),
        )
    # AuthenticationMiddleware: extracts Bearer token, calls verifier
    app = AuthenticationMiddleware(app, backend=BearerAuthBackend(verifier))

    # ── Lifespan Bypass ──
    auth_app = app
    # Paths that bypass auth (no Bearer token needed)
    AUTH_BYPASS_PATHS = {
        "/health",
        "/authorize",
        "/token",
        "/register",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    }

    class LifespanBypass:
        """Routes lifespan events past auth middleware to Starlette.
        Also bypasses auth for health, OAuth discovery, and login routes."""
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "lifespan":
                await starlette_app(scope, receive, send)
            elif scope["type"] == "http":
                path = scope.get("path", "")
                if any(path == p or path.startswith(p + "/") for p in AUTH_BYPASS_PATHS):
                    await starlette_app(scope, receive, send)
                else:
                    await auth_app(scope, receive, send)
            else:
                await auth_app(scope, receive, send)

    app = LifespanBypass()

    mode = "enforced" if AUTH_REQUIRED else "optional"
    log.info("MCP Streamable HTTP auf %s:%s%s (auth: %s, oauth: enabled)", MCP_HOST, MCP_PORT, MCP_PATH, mode)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
