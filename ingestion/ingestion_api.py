"""
Ingestion API – FastAPI Wrapper
================================
HTTP interface for the ingestion pipeline.
Called by the MCP server via the Docker network.

Endpoints:
  POST /scan              — Scan text for PII (without ingestion)
  POST /pseudonymize      — Pseudonymize text without storage (chat path)
  POST /ingest            — Ingest text (full privacy pipeline)
  POST /ingest/chunks     — Ingest pre-processed chunks (adapter, ingest_text_chunks pipeline)
  POST /snapshots/create  — Create a knowledge snapshot
  POST /sync              — Sync all configured Git repositories
  POST /sync/{repo_name}  — Sync a single Git repository
  GET  /health            — Health check
"""

import os
import json
import logging
import uuid
import time
import asyncio
import base64
import binascii
from datetime import datetime, timedelta, timezone
from typing import Any

import secrets

import httpx
import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

from prometheus_client import Counter, Histogram, make_asgi_app as prom_make_asgi_app

from pii_scanner import get_scanner
from snapshot_service import create_snapshot
from content_extraction import (
    ContentExtractor,
    detect_content_type,
    mime_type_to_extension,
    should_skip_file,
)

# ── Configuration ────────────────────────────────────────────
QDRANT_URL   = os.getenv("QDRANT_URL",   "http://qdrant:6333")
OPA_URL      = os.getenv("OPA_URL",       "http://opa:8181")
RERANKER_URL = os.getenv("RERANKER_URL",  "http://reranker:8082")

# ── Backward-compat fallback ──
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")

# ── Embedding provider ──
EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL        = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY      = os.getenv("EMBEDDING_API_KEY", "")

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.llm_provider import EmbeddingProvider, CompletionProvider
from shared.config import build_postgres_url, read_secret, PG_POOL_MIN, PG_POOL_MAX
from shared.telemetry import (
    init_telemetry, setup_auto_instrumentation, trace_operation,
    request_telemetry_context, get_current_telemetry,
    MetricsAggregator, TELEMETRY_IN_RESPONSE,
)
from shared.pii_verify_provider import (
    create_pii_verify_provider,
    build_candidates_from_locations,
    apply_verdicts_to_scan_result,
    VerifyStats,
)
from shared.opa_client import (
    OpaPolicyMissingError,
    opa_query,
    verify_required_policies,
)
from shared.ingestion_auth import verify_ingestion_auth_configured

POSTGRES_URL = build_postgres_url()

embedding_provider = EmbeddingProvider(
    base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY
)

from shared.embedding_cache import EmbeddingCache
embedding_cache = EmbeddingCache()

# ── LLM / Layer generation provider ──
LLM_PROVIDER_URL = os.getenv("LLM_PROVIDER_URL", os.getenv("OLLAMA_URL", "http://pb-ollama:11434"))
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LAYER_GENERATION_ENABLED = os.getenv("LAYER_GENERATION_ENABLED", "true").lower() == "true"

completion_provider = CompletionProvider(
    base_url=LLM_PROVIDER_URL, api_key=LLM_API_KEY
)

# ── PII Verifier (Presidio precision filter) ──────────────────
# ``noop`` keeps the pre-existing Presidio-only behaviour (community
# default). ``llm`` sends ambiguous candidates (PERSON / LOCATION /
# ORGANIZATION) to the chat endpoint for context-aware filtering.
# The *backend* is OPA-policy-driven at runtime so admins can flip
# via manage_policies without restarting ingestion. The env vars only
# describe WHERE the LLM lives, not WHETHER to call it.
PII_VERIFIER_URL            = os.getenv("PII_VERIFIER_URL", LLM_PROVIDER_URL)
PII_VERIFIER_MODEL          = os.getenv("PII_VERIFIER_MODEL", LLM_MODEL)
PII_VERIFIER_API_KEY        = os.getenv("PII_VERIFIER_API_KEY", LLM_API_KEY)
PII_VERIFIER_ENABLED_DEFAULT = os.getenv("PII_VERIFIER_ENABLED", "false").lower() == "true"
PII_VERIFIER_BACKEND_DEFAULT = os.getenv("PII_VERIFIER_BACKEND", "noop")
PII_VERIFIER_TIMEOUT        = float(os.getenv("PII_VERIFIER_TIMEOUT_SECONDS", "15"))

# Lazily-initialised per-backend singletons. OPA policy picks which one
# runs for a given request; we keep the LLM provider warm to avoid
# reconnect costs on repeated ingests.
_pii_verifier_providers: dict[str, Any] = {}


def _get_pii_verifier_provider(backend: str):
    """Return (and cache) the provider instance for the requested backend."""
    key = (backend or "noop").lower()
    prov = _pii_verifier_providers.get(key)
    if prov is not None:
        return prov
    prov = create_pii_verify_provider(
        backend=key,
        base_url=PII_VERIFIER_URL,
        api_key=PII_VERIFIER_API_KEY,
        model=PII_VERIFIER_MODEL,
    )
    _pii_verifier_providers[key] = prov
    return prov

DEFAULT_COLLECTION = "pb_general"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pb-ingestion")

# ── Prometheus Metrics ───────────────────────────────────────
# Initialize variables first
pb_ingestion_requests = None
pb_ingestion_duration = None
pb_ingestion_chunks = None
pb_ingestion_pii_entities = None
pb_ingestion_embedding_batch = None
pb_ingestion_pii_verifier_calls = None
pb_ingestion_pii_verifier_duration = None

# Try to create metrics, handle duplicate registration gracefully
try:
    pb_ingestion_requests = Counter(
        "pb_ingestion_requests_total", "Ingestion requests", ["endpoint", "status"],
    )
    pb_ingestion_duration = Histogram(
        "pb_ingestion_duration_seconds", "Ingestion request duration", ["endpoint"],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    )
    pb_ingestion_chunks = Counter(
        "pb_ingestion_chunks_total", "Total chunks ingested", ["collection"],
    )
    pb_ingestion_pii_entities = Counter(
        "pb_ingestion_pii_entities_total", "PII entities found", ["entity_type", "action"],
    )
    pb_ingestion_embedding_batch = Histogram(
        "pb_ingestion_embedding_batch_size", "Embedding batch size",
        buckets=[1, 5, 10, 20, 50, 100],
    )
    pb_extract_requests = Counter(
        "pb_extract_requests_total", "Document extraction requests",
        ["status", "extractor"],
    )
    pb_extract_duration = Histogram(
        "pb_extract_duration_seconds", "Document extraction duration",
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
    )
    pb_extract_bytes_in = Histogram(
        "pb_extract_bytes_in", "Document extraction input size in bytes",
        buckets=[1024, 10_000, 100_000, 500_000, 1_000_000, 5_000_000, 10_000_000, 25_000_000],
    )
    pb_ingestion_pii_verifier_calls = Counter(
        "pb_ingestion_pii_verifier_calls_total",
        "Semantic PII verifier decisions (per candidate)",
        ["entity_type", "backend", "result"],
    )
    pb_ingestion_pii_verifier_duration = Histogram(
        "pb_ingestion_pii_verifier_duration_seconds",
        "Semantic PII verifier round-trip duration",
        ["backend"],
        buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    )
except ValueError as e:
    if "Duplicated timeseries" in str(e):
        # Metrics already registered, get them from registry
        from prometheus_client import REGISTRY
        for collector in list(REGISTRY._collector_to_names.keys()):
            if hasattr(collector, '_name'):
                if collector._name == "pb_ingestion_requests_total":
                    pb_ingestion_requests = collector
                elif collector._name == "pb_ingestion_duration_seconds":
                    pb_ingestion_duration = collector
                elif collector._name == "pb_ingestion_chunks_total":
                    pb_ingestion_chunks = collector
                elif collector._name == "pb_ingestion_pii_entities_total":
                    pb_ingestion_pii_entities = collector
                elif collector._name == "pb_ingestion_embedding_batch_size":
                    pb_ingestion_embedding_batch = collector
                elif collector._name == "pb_extract_requests_total":
                    pb_extract_requests = collector
                elif collector._name == "pb_extract_duration_seconds":
                    pb_extract_duration = collector
                elif collector._name == "pb_extract_bytes_in":
                    pb_extract_bytes_in = collector
                elif collector._name == "pb_ingestion_pii_verifier_calls_total":
                    pb_ingestion_pii_verifier_calls = collector
                elif collector._name == "pb_ingestion_pii_verifier_duration_seconds":
                    pb_ingestion_pii_verifier_duration = collector
    else:
        raise

# ── FastAPI App ──────────────────────────────────────────────
app = FastAPI(title="Powerbrain Ingestion API", version="1.0.0")

# ── Service-token auth (B-50, defense-in-depth on top of pb-net) ──
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
    service_name="ingestion",
)
from auth_middleware import IngestionAuthMiddleware  # noqa: E402
app.add_middleware(IngestionAuthMiddleware, expected_token=INGESTION_AUTH_TOKEN)

# ── Telemetry Initialization ─────────────────────────────────
_ingestion_tracer = init_telemetry("pb-ingestion")
setup_auto_instrumentation(app)
_ingestion_metrics = MetricsAggregator("ingestion")

# ── JSON Metrics Endpoint (must be defined before mount) ─────
@app.get("/metrics/json")
async def metrics_json():
    snap = _ingestion_metrics.snapshot()
    response = {
        "service": "ingestion",
        "uptime_seconds": snap["uptime_seconds"],
        "requests": {"total": 0, "ok": 0, "error": 0},
        "chunks": {"total": 0},
        "pii": {"entities_found": {}},
        "embedding": {"batch_total": 0},
    }
    for key, val in snap["raw_metrics"].items():
        if "pb_ingestion_requests_total" in key:
            if "ok" in key:
                response["requests"]["ok"] += val
            elif "error" in key:
                response["requests"]["error"] += val
        elif "pb_ingestion_chunks_total" in key:
            response["chunks"]["total"] += val
        elif "pb_ingestion_pii_entities_total" in key:
            if "entity_type=" in key:
                et = key.split("entity_type=")[1].split(",")[0].split("}")[0]
                response["pii"]["entities_found"][et] = (
                    response["pii"]["entities_found"].get(et, 0) + val
                )
    response["requests"]["total"] = response["requests"]["ok"] + response["requests"]["error"]
    return JSONResponse(content=response)

# ── Mount Prometheus Metrics ─────────────────────────────────
metrics_app = prom_make_asgi_app()
app.mount("/metrics", metrics_app)

# ── Clients (lifecycle-managed) ─────────────────────────────
qdrant: AsyncQdrantClient | None = None
http_client: httpx.AsyncClient | None = None
pg_pool: asyncpg.Pool | None = None

# ── Document extraction (shared singleton, markitdown lazy-init on first use) ──
_content_extractor = ContentExtractor()
EXTRACT_MAX_BYTES = int(os.getenv("EXTRACT_MAX_BYTES", str(25 * 1024 * 1024)))  # 25 MB
EXTRACT_TIMEOUT_SECONDS = float(os.getenv("EXTRACT_TIMEOUT_SECONDS", "30"))


REQUIRED_OPA_POLICIES = [
    "pb/ingestion/quality_gate",
    "pb/privacy",
    "pb/config/ingestion/pii_verifier",
]


@app.on_event("startup")
async def startup():
    global qdrant, http_client, pg_pool
    qdrant = AsyncQdrantClient(url=QDRANT_URL)
    http_client = httpx.AsyncClient(timeout=60.0)
    try:
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX)
        log.info("PostgreSQL pool initialized")
    except Exception as e:
        log.error(f"PostgreSQL connection failed: {e}")
        pg_pool = None

    # Fail loudly if required OPA policies are not loaded — otherwise the
    # runtime helpers would fail-closed with misleading diagnostics
    # (see issue #59: "quality_score 0.629 < required 0.000").
    # Disabled only for test runs where OPA is not reachable.
    if os.getenv("SKIP_OPA_STARTUP_CHECK", "false").lower() != "true":
        try:
            await verify_required_policies(http_client, OPA_URL, REQUIRED_OPA_POLICIES)
        except Exception as exc:
            log.error("OPA startup verification failed: %s", exc)
            raise


@app.on_event("shutdown")
async def shutdown():
    if pg_pool:
        await pg_pool.close()
    if http_client:
        await http_client.aclose()


# ── Request/Response Models ─────────────────────────────────

class IngestRequest(BaseModel):
    source: str
    source_type: str | None = "text"
    collection: str | None = None
    project: str | None = None
    classification: str = "internal"
    metadata: dict[str, Any] = {}


class SnapshotRequest(BaseModel):
    name: str = Field(description="Name of the snapshot")
    description: str = Field(default="", description="Description")
    created_by: str = Field(default="system", description="Created by")


class ScanRequest(BaseModel):
    text: str = Field(min_length=1, description="Text to scan for PII")
    language: str = Field(default="de", description="Language of the text (de, en)")


class ScanResponse(BaseModel):
    contains_pii: bool = Field(description="Whether PII was detected")
    masked_text: str = Field(description="Text with masked PII entities")
    entity_types: list[str] = Field(description="List of detected PII types")


class PseudonymizeRequest(BaseModel):
    text: str = Field(min_length=1, description="Text to pseudonymize")
    salt: str = Field(min_length=1, description="Salt for deterministic pseudonyms")
    language: str = Field(default="de", description="Language of the text (de, en)")


class PseudonymizeResponse(BaseModel):
    text: str = Field(description="Pseudonymized text")
    mapping: dict[str, str] = Field(description="Mapping original → pseudonym")
    contains_pii: bool = Field(description="Whether PII was detected")
    entity_types: list[str] = Field(description="List of detected PII types")


class ChunkIngestRequest(BaseModel):
    """Request for adapter-based chunk ingestion. Internal use only."""
    chunks: list[str]
    project: str
    collection: str = "pb_general"
    classification: str = "internal"
    metadata: dict[str, Any] = {}
    source: str
    source_type: str = "text"


class ExtractRequest(BaseModel):
    """Request for binary document extraction.

    Called primarily by pb-proxy for chat attachments, but also usable by any
    adapter that needs to convert a binary blob to text via the shared pipeline.
    """
    data: str = Field(
        min_length=1,
        description="Base64-encoded raw bytes of the file",
    )
    filename: str = Field(
        min_length=1,
        description="Filename including extension (used to select the extractor)",
    )
    mime_type: str | None = Field(
        default=None,
        description="Optional MIME hint (not authoritative; extension takes precedence)",
    )
    max_bytes: int | None = Field(
        default=None,
        description="Optional per-request size cap. Always capped by EXTRACT_MAX_BYTES.",
    )


class ExtractResponse(BaseModel):
    text: str
    content_type: str
    extractor: str = Field(
        description="Backend used: markitdown | fallback | text | ocr | skipped | failed"
    )
    bytes_in: int
    chars_out: int
    truncated: bool = False


class PreviewRequest(BaseModel):
    """Dry-run request for the pipeline inspector (demo surface).

    Either supply extracted ``text`` directly, or pass base64 ``data``
    plus ``filename`` to run the same extractor that a real ingest
    would use. No data is persisted — the call touches only the
    in-process scanner, quality module, and OPA.
    """
    text:           str | None = None
    data:           str | None = Field(default=None, description="Base64 bytes; alternative to `text`")
    filename:       str | None = None
    mime_type:      str | None = None
    language:       str = Field(default="de")
    classification: str = Field(default="internal")
    source_type:    str = Field(default="default")
    metadata:       dict[str, Any] = Field(default_factory=dict)
    legal_basis:    str | None = Field(
        default=None,
        description="Optional hint for OPA privacy.pii_action on confidential data",
    )


class PreviewResponse(BaseModel):
    """Flattened view of what every pipeline step would do.

    Shape is intentionally optimised for a demo UI — grouped by phase
    with booleans / counts the UI can render as badges.

    ``verifier`` is populated when the semantic PII verifier
    (``pb.config.ingestion.pii_verifier.enabled=true``) ran between
    the raw Presidio scan and the rest of the pipeline. ``scan``
    reflects the post-verifier state so downstream consumers stay
    consistent — the ``verifier.before`` sub-field holds the raw
    Presidio output for comparison in the demo panel.
    """
    extract:  dict = Field(default_factory=dict)
    scan:     dict = Field(default_factory=dict)
    verifier: dict = Field(default_factory=dict)
    quality:  dict = Field(default_factory=dict)
    privacy:  dict = Field(default_factory=dict)
    summary:  dict = Field(default_factory=dict)


# ── Helper Functions ────────────────────────────────────────

async def get_embedding(text: str) -> list[float]:
    """Generates embedding via the configured provider (OpenAI-compat), with cache."""
    cached = embedding_cache.get(text, EMBEDDING_MODEL)
    if cached is not None:
        return cached
    vector = await embedding_provider.embed(http_client, text, EMBEDDING_MODEL)
    embedding_cache.set(text, EMBEDDING_MODEL, vector)
    return vector


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 200) -> list[str]:
    """Simple chunking with overlap for long texts."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


async def get_or_create_project_salt(project: str | None) -> str:
    """Gets or creates a salt for the project from pii_vault.project_salts."""
    if not pg_pool or not project:
        return secrets.token_hex(16)

    row = await pg_pool.fetchrow(
        "SELECT salt FROM pii_vault.project_salts WHERE project_id = $1",
        project,
    )
    if row:
        return row["salt"]

    salt = secrets.token_hex(16)
    try:
        await pg_pool.execute(
            """INSERT INTO pii_vault.project_salts (project_id, salt)
               VALUES ($1, $2)
               ON CONFLICT (project_id) DO NOTHING""",
            project, salt,
        )
    except Exception as e:
        log.warning(f"Project salt creation failed: {e}")
    # Re-read to get the winning salt (handles race condition)
    row = await pg_pool.fetchrow(
        "SELECT salt FROM pii_vault.project_salts WHERE project_id = $1",
        project,
    )
    return row["salt"] if row else salt


async def check_opa_quality_gate(
    source_type: str, quality_score: float
) -> dict:
    """Query OPA pb.ingestion.quality_gate (EU AI Act Art. 10).

    Returns a dict with ``allowed`` (bool), ``min_score`` (float) and
    ``reason`` (str). On OPA failure we fail-closed (allowed=False) so a
    broken policy engine cannot silently bypass the quality gate.

    ``min_score`` uses the sentinel ``-1.0`` when the policy package is
    not loaded or OPA is unreachable. The normal minimum is never
    negative, so ``-1.0`` in logs or the ``ingestion_rejections`` table
    flags a configuration problem rather than a threshold comparison.
    """
    input_data = {
        "source_type":    source_type or "default",
        "quality_score":  float(quality_score),
    }
    try:
        result = await opa_query(
            http_client, OPA_URL, "pb/ingestion/quality_gate", input_data,
        )
    except OpaPolicyMissingError as exc:
        log.error("OPA policy missing: %s", exc.package_path)
        return {
            "allowed":   False,
            "min_score": -1.0,
            "reason":    f"opa_policy_missing: {exc.package_path}",
        }
    except Exception as e:
        log.warning("OPA quality_gate check failed, fail-closed: %s", e)
        return {"allowed": False, "min_score": -1.0,
                "reason": f"opa_unreachable: {e}"}

    if not isinstance(result, dict):
        log.warning("OPA quality_gate returned non-dict %r, fail-closed", result)
        return {"allowed": False, "min_score": -1.0,
                "reason": "opa_unexpected_shape"}

    return {
        "allowed":   bool(result.get("allowed", False)),
        "min_score": float(result.get("min_score", 0.0)),
        "reason":    result.get("reason", ""),
    }


async def check_opa_pii_verifier() -> dict:
    """Fetch the semantic verifier policy from OPA.

    Returns ``{enabled, backend, min_confidence_keep}``. Defaults match
    the noop backend so an outage can't accidentally widen what the
    verifier drops — fail-closed on policy unreachability.
    """
    fallback = {
        "enabled":  PII_VERIFIER_ENABLED_DEFAULT,
        "backend":  PII_VERIFIER_BACKEND_DEFAULT,
        "min_confidence_keep": 0.5,
    }
    try:
        data = await opa_query(
            http_client, OPA_URL, "pb/config/ingestion/pii_verifier",
        )
    except OpaPolicyMissingError as exc:
        log.warning(
            "OPA policy %s not loaded — falling back to env defaults "
            "(enabled=%s, backend=%s)",
            exc.package_path, fallback["enabled"], fallback["backend"],
        )
        return fallback
    except Exception as exc:
        log.warning("OPA pii_verifier policy lookup failed, using env defaults: %s", exc)
        return fallback

    if not isinstance(data, dict):
        return fallback
    return {
        "enabled":  bool(data.get("enabled", fallback["enabled"])),
        "backend":  str(data.get("backend", fallback["backend"])),
        "min_confidence_keep": float(
            data.get("min_confidence_keep", fallback["min_confidence_keep"])
        ),
    }


async def apply_pii_verifier(
    text: str,
    contains_pii: bool,
    entity_counts: dict[str, int],
    entity_locations: list[dict],
) -> tuple[bool, dict[str, int], list[dict], dict]:
    """Run the verifier on a scan result, returning filtered data + stats.

    Wraps :meth:`_BasePIIVerifyProvider.verify` with OPA policy,
    Prometheus counters, and the telemetry trace span. Safe to call
    unconditionally: when the verifier is disabled (noop backend) the
    returned arrays are unchanged and ``stats["enabled"]`` is False.
    """
    stats_dict: dict = {"enabled": False, "backend": "noop",
                         "input_count": len(entity_locations)}

    policy = await check_opa_pii_verifier()
    if not policy["enabled"] or policy["backend"] == "noop" or not entity_locations:
        return contains_pii, entity_counts, entity_locations, {
            **stats_dict,
            "enabled": policy["enabled"],
            "backend": policy["backend"],
        }

    # Build candidates + run. Provider handles pattern vs ambiguous split.
    # The *backend* is decided by OPA, so the singleton is looked up per
    # call — admins can flip from noop → llm at runtime without needing
    # an ingestion restart.
    candidates = build_candidates_from_locations(text, entity_locations)
    provider = _get_pii_verifier_provider(policy["backend"])
    t0 = time.perf_counter()
    try:
        with trace_operation(_ingestion_tracer, "pii_verify", "ingestion",
                             backend=policy["backend"],
                             input_count=len(candidates)):
            keep, stats = await provider.verify(
                http_client, text, candidates,
            )
    except Exception as exc:
        log.warning("pii_verify_provider raised — falling back to noop: %s", exc)
        return contains_pii, entity_counts, entity_locations, {
            **stats_dict, "enabled": True, "backend": policy["backend"],
            "error": str(exc),
        }
    duration = time.perf_counter() - t0

    # Prometheus
    if pb_ingestion_pii_verifier_duration:
        pb_ingestion_pii_verifier_duration.labels(backend=stats.backend).observe(duration)
    if pb_ingestion_pii_verifier_calls:
        for etype, bucket in stats.by_entity_type.items():
            for result_name in ("kept", "reverted", "forwarded"):
                count = int(bucket.get(result_name, 0))
                if count:
                    pb_ingestion_pii_verifier_calls.labels(
                        entity_type=etype, backend=stats.backend,
                        result=result_name,
                    ).inc(count)

    new_contains, new_counts, new_locs = apply_verdicts_to_scan_result(
        entity_counts, entity_locations, keep,
    )
    return new_contains, new_counts, new_locs, {
        "enabled":       True,
        "backend":       stats.backend,
        "input_count":   stats.input_count,
        "forwarded":     stats.forwarded,
        "reviewed":      stats.reviewed,
        "kept":          stats.kept,
        "reverted":      stats.reverted,
        "errors":        stats.errors,
        "duration_ms":   round(duration * 1000, 2),
        "by_entity_type": stats.by_entity_type,
    }


async def check_opa_privacy(
    classification: str, contains_pii: bool, legal_basis: str | None = None
) -> dict:
    """Queries OPA for pii_action and dual_storage_enabled.

    OPA endpoint: /v1/data/pb/privacy. Fail-closed on missing policy or
    unreachable OPA — privacy decisions must never silently default
    to a more permissive action than ``block``.
    """
    input_data = {
        "classification": classification,
        "contains_pii": contains_pii,
        "legal_basis": legal_basis or "",
    }
    result = {"pii_action": "block", "dual_storage_enabled": False}
    try:
        data = await opa_query(http_client, OPA_URL, "pb/privacy", input_data)
    except OpaPolicyMissingError as exc:
        log.error(
            "OPA privacy policy not loaded (%s) — defaulting to block",
            exc.package_path,
        )
        result["reason"] = f"opa_policy_missing: {exc.package_path}"
        return result
    except Exception as e:
        log.warning("OPA privacy check failed, defaulting to block: %s", e)
        return result

    if isinstance(data, dict):
        result["pii_action"] = data.get("pii_action", "block")
        result["dual_storage_enabled"] = data.get("dual_storage_enabled", False)
        result["retention_days"] = data.get("retention_days", 365)
    return result


async def store_in_vault(
    doc_id: str,
    chunk_index: int,
    original_text: str,
    pii_entities: list[dict],
    mapping: dict[str, str],
    salt: str,
    retention_days: int,
    data_category: str | None,
) -> str:
    """Stores original text + mapping in pii_vault. Returns vault_ref UUID."""
    if not pg_pool:
        raise RuntimeError("PostgreSQL unavailable for vault storage")

    vault_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            # Store original
            await conn.execute("""
                INSERT INTO pii_vault.original_content
                    (id, document_id, chunk_index, original_text,
                     pii_entities, retention_expires_at, data_category)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, vault_id, doc_id, chunk_index, original_text,
                json.dumps(pii_entities), expires_at, data_category)

            # Store mapping (one entry per entity)
            for original, pseudonym in mapping.items():
                entity_type = "UNKNOWN"
                for e in pii_entities:
                    if e.get("text") == original:
                        entity_type = e.get("type", "UNKNOWN")
                        break
                await conn.execute("""
                    INSERT INTO pii_vault.pseudonym_mapping
                        (document_id, chunk_index, pseudonym,
                         entity_type, salt)
                    VALUES ($1, $2, $3, $4, $5)
                """, doc_id, chunk_index, pseudonym, entity_type, salt)

    return vault_id


async def log_pii_scan(
    source: str,
    entities_found: dict,
    action_taken: str,
    classification: str,
    dataset_id: str | None = None,
):
    """Writes an entry to pii_scan_log."""
    if not pg_pool:
        return
    try:
        await pg_pool.execute("""
            INSERT INTO pii_scan_log
                (source, entities_found, action_taken, classification, dataset_id)
            VALUES ($1, $2, $3, $4, $5)
        """, source, json.dumps(entities_found), action_taken,
            classification, dataset_id)
    except Exception as e:
        log.warning(f"pii_scan_log insert failed: {e}")


L0_SYSTEM_PROMPT = (
    "You are a document abstraction engine. Generate a single-sentence abstract "
    "(max 100 tokens) that captures the essence of the document. The abstract must "
    "enable quick relevance assessment. Do not include specific details — only the "
    "topic and scope. Respond with the abstract only, no preamble."
)

L1_SYSTEM_PROMPT = (
    "You are a document overview engine. Generate a structured Markdown overview "
    "(max 500 tokens) that covers:\n"
    "1. What this document is about (1 sentence)\n"
    "2. Key sections/topics as bullet points\n"
    "3. Most important facts or numbers\n"
    "4. What kind of detailed information is available in the full document\n\n"
    "The overview enables an AI agent to decide whether to load the full document. "
    "Respond with the overview only, no preamble. Use Markdown formatting."
)


async def generate_l0(chunks: list[str], source: str = "", classification: str = "") -> str | None:
    """Generate a short L0 abstract (~100 tokens) from document chunks.

    Returns None if LLM is unavailable or generation fails (graceful degradation).
    """
    if not LAYER_GENERATION_ENABLED:
        return None
    try:
        full_text = "\n\n".join(chunks)
        # Truncate to ~4000 chars to stay within context limits
        if len(full_text) > 4000:
            full_text = full_text[:4000] + "\n\n[truncated]"
        user_prompt = (
            f"Document source: {source}\n"
            f"Classification: {classification}\n"
            f"Full text (from {len(chunks)} chunks):\n\n{full_text}"
        )
        result = await completion_provider.generate(
            http_client,
            model=LLM_MODEL,
            system_prompt=L0_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        return result
    except Exception as e:
        log.warning(f"L0 generation failed (graceful degradation): {e}")
        return None


async def generate_l1(chunks: list[str], source: str = "", classification: str = "") -> str | None:
    """Generate a structured L1 Markdown overview (~500 tokens) from document chunks.

    Returns None if LLM is unavailable or generation fails (graceful degradation).
    """
    if not LAYER_GENERATION_ENABLED:
        return None
    try:
        full_text = "\n\n".join(chunks)
        # Truncate to ~8000 chars to allow more detail for overview
        if len(full_text) > 8000:
            full_text = full_text[:8000] + "\n\n[truncated]"
        user_prompt = (
            f"Document source: {source}\n"
            f"Classification: {classification}\n"
            f"Full text (from {len(chunks)} chunks):\n\n{full_text}"
        )
        result = await completion_provider.generate(
            http_client,
            model=LLM_MODEL,
            system_prompt=L1_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        return result
    except Exception as e:
        log.warning(f"L1 generation failed (graceful degradation): {e}")
        return None


async def ingest_text_chunks(
    chunks: list[str],
    collection: str,
    source: str,
    classification: str,
    project: str | None,
    metadata: dict[str, Any],
    source_type: str = "text",
) -> dict:
    """Vectorizes chunks and stores them in Qdrant + PostgreSQL.

    Pipeline:
    1. PII scan of each chunk
    2. OPA policy: pii_action + dual_storage_enabled (once per document)
    3. Depending on action: mask, pseudonymize+vault, or block
    4. Embed + Qdrant upsert
    5. PostgreSQL metadata
    """
    scanner = get_scanner()
    points = []
    pii_detected = False
    vault_refs: list[str | None] = []
    doc_id = str(uuid.uuid4())
    processed_texts: list[str] = []
    chunk_metadata: list[dict] = []

    # Pre-create documents_meta (so that vault FK constraints are satisfied)
    if pg_pool:
        try:
            await pg_pool.execute("""
                INSERT INTO documents_meta
                    (id, title, source, qdrant_collection, classification,
                     chunk_count, contains_pii, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
                doc_id,
                source[:200],
                source,
                collection,
                classification,
                0,  # chunk_count updated later
                False,  # contains_pii updated later
                json.dumps(metadata),
            )
        except Exception as e:
            log.error(f"PG documents_meta insert failed: {e}")

    # Query OPA policy once per document (classification is the same for all chunks)
    opa_result: dict | None = None
    total_pii_entities = 0

    for i, chunk in enumerate(chunks):
        # 1. PII-Scan
        scan_result = scanner.scan_text(chunk)
        vault_ref = None

        # Apply semantic verifier when policy enables it. Noop-default
        # preserves the pre-existing behaviour for every community
        # deployment.
        if scan_result.contains_pii:
            contains_v, counts_v, locs_v, _vstats = await apply_pii_verifier(
                chunk,
                scan_result.contains_pii,
                dict(scan_result.entity_counts),
                list(scan_result.entity_locations),
            )
            # Replace the scan_result view downstream uses with the
            # verifier-filtered one (or keep the original when disabled).
            scan_result = type(scan_result)(
                contains_pii=contains_v,
                entity_counts=counts_v,
                entity_locations=locs_v,
            )

        if scan_result.contains_pii:
            pii_detected = True
            total_pii_entities += sum(int(c) for c in scan_result.entity_counts.values())

            # Track PII entities found
            for entity_type, count in scan_result.entity_counts.items():
                for _ in range(int(count)):
                    pb_ingestion_pii_entities.labels(entity_type=entity_type, action="found").inc() if pb_ingestion_pii_entities else None

            # 2. OPA Policy: What to do with PII? (only query on first detection)
            if opa_result is None:
                opa_result = await check_opa_privacy(
                    classification, True, metadata.get("legal_basis")
                )
            pii_action = opa_result["pii_action"]
            dual_storage = opa_result["dual_storage_enabled"]
            retention_days = opa_result.get("retention_days", 365)

            if pii_action == "block":
                log.warning(
                    f"PII detected in chunk {i}, classification '{classification}'"
                    f" → blocked by OPA policy"
                )
                await log_pii_scan(
                    source, scan_result.entity_counts, "block", classification
                )
                return {
                    "status": "blocked",
                    "reason": f"PII in {classification} data blocked by policy",
                    "chunks_ingested": 0,
                    "pii_detected": True,
                }

            elif pii_action in ("pseudonymize", "encrypt_and_store") and dual_storage:
                # 3a. Dual Storage: pseudonymize + store original in vault
                log.info(
                    f"PII in chunk {i}: {scan_result.entity_counts}"
                    f" → pseudonymizing (dual storage, action={pii_action})"
                )
                salt = await get_or_create_project_salt(project)
                pseudo_text, mapping = scanner.pseudonymize_text(chunk, salt)

                # Vault: Store original + mapping
                pii_entities = [
                    {
                        "type": loc["type"],
                        "text": chunk[loc["start"]:loc["end"]],
                        "start": loc["start"],
                        "end": loc["end"],
                        "score": loc["score"],
                    }
                    for loc in scan_result.entity_locations
                ]

                if pg_pool:
                    vault_ref = await store_in_vault(
                        doc_id, i, chunk, pii_entities, mapping,
                        salt, retention_days,
                        metadata.get("data_category"),
                    )

                chunk = pseudo_text
                await log_pii_scan(
                    source, scan_result.entity_counts,
                    "pseudonymize", classification,
                )

            else:
                # 3b. Fallback: mask (public or dual_storage=false)
                if pii_action not in ("mask", "pseudonymize"):
                    log.warning(
                        f"PII action '{pii_action}' not fully implemented, "
                        f"falling back to mask"
                    )
                log.warning(
                    f"PII in chunk {i}: {scan_result.entity_counts} → masking"
                )
                chunk = scanner.mask_text(chunk)
                await log_pii_scan(
                    source, scan_result.entity_counts, "mask", classification
                )

        # 4. Collect processed chunk for batch embedding
        processed_texts.append(chunk)
        chunk_metadata.append({
            "vault_ref": vault_ref,
            "contains_pii": scan_result.contains_pii,
            "chunk_index": i,
        })

    # 4a. Data-Quality Gate (EU AI Act Art. 10) ──────────────────────
    # Score the post-PII-scan text so pseudonymization does not inflate
    # the PII-density factor, then ask OPA whether the document passes.
    try:
        from quality import compute_quality_score
        joined_text = "\n\n".join(processed_texts)
        quality_report = compute_quality_score(
            joined_text,
            metadata={
                "source":         source,
                "classification": classification,
                "project":        project,
                "legal_basis":    metadata.get("legal_basis"),
                "data_category":  metadata.get("data_category"),
            },
            source_type=source_type,
            pii_entity_count=total_pii_entities,
        )
    except Exception as e:
        log.warning(f"Quality scoring failed, treating as score=0: {e}")
        from quality import QualityReport
        quality_report = QualityReport(score=0.0,
                                       factors={"error": 0.0},
                                       language="unknown")

    gate = await check_opa_quality_gate(source_type, quality_report.score)

    if not gate["allowed"]:
        # `min_score == -1.0` is the sentinel for "policy missing / OPA
        # unreachable" — log at ERROR so the cause is obvious. Any other
        # threshold rejection is normal.
        _log = log.error if gate["min_score"] < 0 else log.warning
        _log(
            "Quality gate rejected document source=%r score=%.3f min=%.3f reason=%r",
            source, quality_report.score, gate["min_score"], gate["reason"],
        )
        # Audit the rejection and remove the pre-created documents_meta row.
        if pg_pool:
            try:
                await pg_pool.execute(
                    """
                    INSERT INTO ingestion_rejections
                        (source_type, project, classification,
                         quality_score, min_required, reason,
                         quality_details, sample_snippet)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    source_type,
                    project,
                    classification,
                    quality_report.score,
                    gate["min_score"],
                    gate["reason"] or "quality_gate_denied",
                    json.dumps(quality_report.to_dict()),
                    (processed_texts[0][:500] if processed_texts else None),
                )
                await pg_pool.execute(
                    "DELETE FROM documents_meta WHERE id = $1", doc_id,
                )
            except Exception as e:
                log.error(f"Failed to persist ingestion rejection: {e}")
        return {
            "status":        "rejected",
            "reason":        gate["reason"] or "quality_gate_denied",
            "quality_score": round(quality_report.score, 4),
            "min_required":  round(gate["min_score"], 4),
            "details":       quality_report.to_dict(),
            "chunks_ingested": 0,
            "pii_detected":    pii_detected,
        }

    # Passed the gate — persist the score on documents_meta.
    if pg_pool:
        try:
            await pg_pool.execute(
                "UPDATE documents_meta SET quality_score = $1, "
                "quality_details = $2 WHERE id = $3",
                quality_report.score,
                json.dumps(quality_report.to_dict()),
                doc_id,
            )
        except Exception as e:
            log.warning(f"Failed to persist quality score: {e}")

    # 4b. Batch embedding (cache-aware)
    embeddings: list[list[float]] = []
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    for idx, text in enumerate(processed_texts):
        cached = embedding_cache.get(text, EMBEDDING_MODEL)
        if cached is not None:
            embeddings.append(cached)
        else:
            embeddings.append([])  # placeholder
            uncached_indices.append(idx)
            uncached_texts.append(text)

    if uncached_texts:
        batch_results = await embedding_provider.embed_batch(
            http_client, uncached_texts, EMBEDDING_MODEL
        )
        # Track embedding batch size
        pb_ingestion_embedding_batch.observe(len(uncached_texts)) if pb_ingestion_embedding_batch else None
        for pos, idx in enumerate(uncached_indices):
            embeddings[idx] = batch_results[pos]
            embedding_cache.set(uncached_texts[pos], EMBEDDING_MODEL, batch_results[pos])

    # 4c. Build points from batch results
    for idx, (text, emb, meta) in enumerate(zip(processed_texts, embeddings, chunk_metadata)):
        point_id = str(uuid.uuid4())
        payload = {
            "text": text,
            "source": source,
            "source_type": source_type,
            "classification": classification,
            "project": project or "",
            "chunk_index": meta["chunk_index"],
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "contains_pii": meta["contains_pii"],
            "vault_ref": meta["vault_ref"],
            "layer": "L2",
            "doc_id": doc_id,
            **metadata,
        }
        points.append(PointStruct(
            id=point_id, vector=emb, payload=payload,
        ))
        vault_refs.append(meta["vault_ref"])

    # 5. In Qdrant upserten (L2 chunks)
    if points:
        await qdrant.upsert(collection_name=collection, points=points)
        log.info(f"{len(points)} L2 points inserted into '{collection}'")
        # Track chunks ingested
        pb_ingestion_chunks.labels(collection=collection).inc(len(points)) if pb_ingestion_chunks else None

    # 6. Generate L0/L1 layers and upsert as separate Qdrant points
    l0_point_id: str | None = None
    l1_point_id: str | None = None

    if points:
        # Collect the final (possibly pseudonymized/masked) chunk texts for LLM input
        processed_chunks = [p.payload["text"] for p in points]
        now_iso = datetime.now(timezone.utc).isoformat()

        # L0 + L1 generation runs in parallel: both call the same stateless
        # completion_provider over the async-safe httpx client, with no shared
        # mutable state. Each helper has its own try/except returning None on
        # failure, so gather() never raises here.
        l0_text, l1_text = await asyncio.gather(
            generate_l0(processed_chunks, source=source, classification=classification),
            generate_l1(processed_chunks, source=source, classification=classification),
        )

        # L0: Abstract
        if l0_text:
            try:
                l0_embedding = await get_embedding(l0_text)
                l0_point_id = str(uuid.uuid4())
                l0_point = PointStruct(
                    id=l0_point_id,
                    vector=l0_embedding,
                    payload={
                        "text": l0_text,
                        "source": source,
                        "source_type": source_type,
                        "classification": classification,
                        "project": project or "",
                        "chunk_index": 0,
                        "ingested_at": now_iso,
                        "contains_pii": False,
                        "vault_ref": None,
                        "layer": "L0",
                        "doc_id": doc_id,
                        **metadata,
                    },
                )
                await qdrant.upsert(collection_name=collection, points=[l0_point])
                log.info(f"L0 abstract upserted for doc {doc_id}")
            except Exception as e:
                log.warning(f"L0 upsert failed (graceful degradation): {e}")
                l0_point_id = None

        # L1: Overview
        if l1_text:
            try:
                l1_embedding = await get_embedding(l1_text)
                l1_point_id = str(uuid.uuid4())
                l1_point = PointStruct(
                    id=l1_point_id,
                    vector=l1_embedding,
                    payload={
                        "text": l1_text,
                        "source": source,
                        "source_type": source_type,
                        "classification": classification,
                        "project": project or "",
                        "chunk_index": 0,
                        "ingested_at": now_iso,
                        "contains_pii": False,
                        "vault_ref": None,
                        "layer": "L1",
                        "doc_id": doc_id,
                        **metadata,
                    },
                )
                await qdrant.upsert(collection_name=collection, points=[l1_point])
                log.info(f"L1 overview upserted for doc {doc_id}")
            except Exception as e:
                log.warning(f"L1 upsert failed (graceful degradation): {e}")
                l1_point_id = None

    # 7. Update documents_meta with final values
    if pg_pool:
        try:
            await pg_pool.execute("""
                UPDATE documents_meta
                SET chunk_count = $2,
                    contains_pii = $3,
                    metadata = $4,
                    l0_point_id = $5,
                    l1_point_id = $6
                WHERE id = $1
            """,
                doc_id,
                len(points),
                pii_detected,
                json.dumps({
                    **metadata,
                    "pii_detected": pii_detected,
                    "vault_refs": [v for v in vault_refs if v],
                }),
                uuid.UUID(l0_point_id) if l0_point_id else None,
                uuid.UUID(l1_point_id) if l1_point_id else None,
            )
        except Exception as e:
            log.error(f"PG documents_meta update failed: {e}")

    return {
        "status": "ok",
        "collection": collection,
        "chunks_ingested": len(points),
        "pii_detected": pii_detected,
        "dual_storage": any(v is not None for v in vault_refs),
        "l0_point_id": l0_point_id,
        "l1_point_id": l1_point_id,
    }


# ── Endpoints ────────────────────────────────────────────────

@app.post("/scan")
async def scan(req: ScanRequest) -> ScanResponse:
    """Scans text for PII without ingestion.

    Called by the MCP server before audit log entries
    with query text are written.
    """
    t0 = time.perf_counter()
    try:
        scanner = get_scanner()
        scan_result = scanner.scan_text(req.text, language=req.language)

        # Track PII entities found
        for entity_type, count in scan_result.entity_counts.items():
            for _ in range(int(count)):
                pb_ingestion_pii_entities.labels(entity_type=entity_type, action="scan").inc() if pb_ingestion_pii_entities else None

        if scan_result.contains_pii:
            masked = scanner.mask_text(req.text, language=req.language)
            entity_types = list(scan_result.entity_counts.keys())
        else:
            masked = req.text
            entity_types = []

        pb_ingestion_requests.labels(endpoint="scan", status="ok").inc() if pb_ingestion_requests else None
        return ScanResponse(
            contains_pii=scan_result.contains_pii,
            masked_text=masked,
            entity_types=entity_types,
        )
    except Exception:
        pb_ingestion_requests.labels(endpoint="scan", status="error").inc() if pb_ingestion_requests else None
        raise
    finally:
        pb_ingestion_duration.labels(endpoint="scan").observe(time.perf_counter() - t0) if pb_ingestion_duration else None


@app.post("/pseudonymize")
async def pseudonymize(req: PseudonymizeRequest) -> PseudonymizeResponse:
    """Pseudonymizes PII in text without storage.

    Called by pb-proxy before chat messages
    are sent to the LLM provider.
    """
    scanner = get_scanner()
    scan_result = scanner.scan_text(req.text, language=req.language)

    if scan_result.contains_pii:
        pseudonymized, mapping = scanner.pseudonymize_text(
            req.text, salt=req.salt, language=req.language,
        )
        entity_types = list(scan_result.entity_counts.keys())
    else:
        pseudonymized = req.text
        mapping = {}
        entity_types = []

    return PseudonymizeResponse(
        text=pseudonymized,
        mapping=mapping,
        contains_pii=scan_result.contains_pii,
        entity_types=entity_types,
    )


@app.post("/extract", response_model=ExtractResponse)
async def extract_document(req: ExtractRequest) -> ExtractResponse:
    """Extract text from a binary document (PDF, DOCX, XLSX, PPTX, MSG, ...).

    Called by pb-proxy for chat attachments and by adapters that need to
    convert binary blobs to text. Runs the sync markitdown call off the
    event loop so the service stays responsive.
    """
    t0 = time.perf_counter()
    extractor_used = "unknown"
    status_label = "error"

    try:
        # ── Pre-decode size guard (reject giant base64 strings before we
        #    allocate the decoded bytes — base64 is ~4/3× the raw size, so
        #    this caps the encoded input at 4/3× EXTRACT_MAX_BYTES). ──────
        encoded_cap = (EXTRACT_MAX_BYTES * 4) // 3 + 16  # +16 padding slack
        if len(req.data) > encoded_cap:
            status_label = "too_large"
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Encoded payload too large ({len(req.data)} > {encoded_cap} chars). "
                    f"Raw document must not exceed {EXTRACT_MAX_BYTES} bytes."
                ),
            )

        # ── Decode base64 ────────────────────────────────────────
        try:
            raw = base64.b64decode(req.data, validate=False)
        except (binascii.Error, ValueError) as exc:
            status_label = "bad_base64"
            raise HTTPException(status_code=400, detail=f"Invalid base64 payload: {exc}")

        bytes_in = len(raw)
        if pb_extract_bytes_in:
            pb_extract_bytes_in.observe(bytes_in)

        # ── Size caps ───────────────────────────────────────────
        hard_cap = EXTRACT_MAX_BYTES
        effective_cap = min(req.max_bytes, hard_cap) if req.max_bytes else hard_cap
        if bytes_in > effective_cap:
            status_label = "too_large"
            raise HTTPException(
                status_code=413,
                detail=f"Payload exceeds limit ({bytes_in} > {effective_cap} bytes)",
            )

        # ── File-type gating ────────────────────────────────────
        filename = req.filename.strip()
        if not filename:
            status_label = "bad_filename"
            raise HTTPException(
                status_code=400,
                detail="Filename must not be empty after trimming whitespace",
            )
        if should_skip_file(filename):
            status_label = "skipped"
            raise HTTPException(
                status_code=415,
                detail=f"File type not supported for extraction: {filename}",
            )

        # If the caller provided a mime type but no extension, append one
        if "." not in os.path.basename(filename) and req.mime_type:
            ext = mime_type_to_extension(req.mime_type)
            if ext:
                filename = filename + ext

        # ── Run extraction (sync markitdown in thread; bounded by timeout) ──
        async def _run_extraction() -> tuple[str | None, str]:
            return await asyncio.to_thread(
                _content_extractor.extract_from_bytes_detailed, raw, filename
            )

        with trace_operation(
            _ingestion_tracer, "extract", "ingestion",
            mime_type=req.mime_type or "", bytes_in=bytes_in,
        ):
            try:
                text, extractor_used = await asyncio.wait_for(
                    _run_extraction(), timeout=EXTRACT_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                status_label = "timeout"
                raise HTTPException(
                    status_code=504,
                    detail=f"Extraction timed out after {EXTRACT_TIMEOUT_SECONDS:.0f}s",
                )
            except HTTPException:
                raise
            except Exception as exc:
                log.warning("Extraction error for %s: %s", filename, exc, exc_info=True)
                status_label = "error"
                raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")

        if extractor_used == "skipped":
            status_label = "skipped"
            raise HTTPException(
                status_code=415,
                detail=f"File type not supported for extraction: {filename}",
            )

        if not text or not text.strip():
            status_label = "empty"
            raise HTTPException(
                status_code=422,
                detail=f"Extraction produced no text ({extractor_used})",
            )

        status_label = "ok"
        return ExtractResponse(
            text=text,
            content_type=detect_content_type(filename),
            extractor=extractor_used,
            bytes_in=bytes_in,
            chars_out=len(text),
            truncated=False,
        )
    finally:
        if pb_extract_requests:
            pb_extract_requests.labels(status=status_label, extractor=extractor_used).inc()
        if pb_extract_duration:
            pb_extract_duration.observe(time.perf_counter() - t0)


@app.post("/preview", response_model=PreviewResponse)
async def preview(req: PreviewRequest) -> PreviewResponse:
    """Dry-run the ingestion pipeline without persisting.

    Powers the sales-demo Pipeline Inspector: a decision-maker uploads
    a representative document (or picks a fixture) and sees exactly
    what each phase — extract, PII scan, quality gate, OPA privacy
    decision — would do, with timings. Nothing is written to
    PostgreSQL or Qdrant; the call is idempotent and cheap.

    Input is either pre-extracted ``text`` or base64 ``data`` +
    ``filename`` for end-to-end visualisation starting from the
    binary.
    """
    overall_t0 = time.perf_counter()

    # ── Phase 1: extract (only if a binary was supplied) ────────
    extract_info: dict[str, Any] = {"status": "skipped"}
    text = req.text or ""
    if not text:
        if not (req.data and req.filename):
            raise HTTPException(
                status_code=400,
                detail="provide either 'text' or ('data' + 'filename')",
            )
        try:
            raw = base64.b64decode(req.data, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400,
                                detail=f"Invalid base64 payload: {exc}")
        if len(raw) > EXTRACT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Payload exceeds limit ({len(raw)} > {EXTRACT_MAX_BYTES} bytes)",
            )
        filename = (req.filename or "").strip()
        t_ex = time.perf_counter()
        try:
            text, extractor = await asyncio.wait_for(
                asyncio.to_thread(
                    _content_extractor.extract_from_bytes_detailed,
                    raw, filename,
                ),
                timeout=EXTRACT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504,
                                detail="Extraction timed out")
        extract_info = {
            "status":     "ok" if text else "empty",
            "extractor":  extractor,
            "bytes_in":   len(raw),
            "chars_out":  len(text or ""),
            "duration_ms": round((time.perf_counter() - t_ex) * 1000, 2),
        }
        if not text:
            # Nothing to scan — return the extraction summary and stop.
            return PreviewResponse(
                extract=extract_info,
                scan={"status": "skipped", "reason": "empty extraction"},
                quality={"status": "skipped"},
                privacy={"status": "skipped"},
                summary={"would_ingest": False,
                         "reason": "extractor produced no text",
                         "duration_ms": round(
                             (time.perf_counter() - overall_t0) * 1000, 2)},
            )

    # ── Phase 2: PII scan (deterministic, no network) ──────────
    t_scan = time.perf_counter()
    scanner = get_scanner()
    scan_result = scanner.scan_text(text, language=req.language)
    raw_contains = scan_result.contains_pii
    raw_counts = dict(scan_result.entity_counts)
    raw_locations = list(scan_result.entity_locations)
    scan_duration_ms = round((time.perf_counter() - t_scan) * 1000, 2)

    # ── Phase 2b: semantic verifier (OPA-gated; community default=off) ──
    contains_after, counts_after, locs_after, verifier_info = \
        await apply_pii_verifier(text, raw_contains, raw_counts, raw_locations)
    if verifier_info.get("enabled"):
        verifier_info["before"] = {
            "contains_pii":  raw_contains,
            "entity_counts": raw_counts,
        }

    scan_info = {
        "contains_pii":     contains_after,
        "entity_counts":    counts_after,
        "entity_locations": locs_after[:20],  # cap UI noise
        "duration_ms":      scan_duration_ms,
    }
    # Replace scan_result fields so downstream quality scoring sees
    # the post-verifier entity density (matches what /ingest will use).
    scan_result = type(scan_result)(
        contains_pii=contains_after,
        entity_counts=counts_after,
        entity_locations=locs_after,
    )

    # ── Phase 3: quality gate (standalone compute + OPA check) ──
    t_q = time.perf_counter()
    try:
        from quality import compute_quality_score  # local import mirrors /ingest
        quality_report = compute_quality_score(
            text,
            metadata=req.metadata,
            source_type=req.source_type,
            pii_entity_count=sum(scan_result.entity_counts.values()),
        )
        gate = await check_opa_quality_gate(req.source_type, quality_report.score)
        quality_info = {
            **quality_report.to_dict(),
            "gate_allowed":   bool(gate.get("allowed")),
            "gate_min_score": gate.get("min_score"),
            "gate_reason":    gate.get("reason"),
            "duration_ms":    round((time.perf_counter() - t_q) * 1000, 2),
        }
    except Exception as exc:
        quality_info = {
            "error":       str(exc),
            "duration_ms": round((time.perf_counter() - t_q) * 1000, 2),
        }

    # ── Phase 4: OPA privacy decision ───────────────────────────
    t_p = time.perf_counter()
    try:
        priv = await check_opa_privacy(
            req.classification, scan_result.contains_pii, req.legal_basis,
        )
        privacy_info = {
            "classification":       req.classification,
            "pii_action":           priv.get("pii_action", "unknown"),
            "dual_storage_enabled": bool(priv.get("dual_storage_enabled")),
            "retention_days":       priv.get("retention_days"),
            "legal_basis_supplied": bool(req.legal_basis),
            "duration_ms":          round((time.perf_counter() - t_p) * 1000, 2),
        }
    except Exception as exc:
        privacy_info = {
            "error":       str(exc),
            "duration_ms": round((time.perf_counter() - t_p) * 1000, 2),
        }

    pii_action = privacy_info.get("pii_action")
    gate_allowed = quality_info.get("gate_allowed", False)
    would_ingest = (
        gate_allowed
        and pii_action not in (None, "block")
    )
    reason_parts: list[str] = []
    if not gate_allowed:
        reason_parts.append(
            f"quality gate: {quality_info.get('gate_reason', 'denied')}"
        )
    if pii_action == "block":
        reason_parts.append("pii_action=block")

    summary = {
        "would_ingest": would_ingest,
        "pii_action":   pii_action,
        "target_collection": req.metadata.get("collection") or "pb_general",
        "reasons":      reason_parts,
        "duration_ms":  round((time.perf_counter() - overall_t0) * 1000, 2),
        "chars":        len(text),
    }

    return PreviewResponse(
        extract=extract_info,
        scan=scan_info,
        verifier=verifier_info,
        quality=quality_info,
        privacy=privacy_info,
        summary=summary,
    )


@app.get("/health")
async def health():
    checks = {"status": "ok", "services": {}}

    # Qdrant
    try:
        await http_client.get(f"{QDRANT_URL}/healthz")
        checks["services"]["qdrant"] = "ok"
    except Exception:
        checks["services"]["qdrant"] = "error"

    # PostgreSQL
    if pg_pool:
        try:
            await pg_pool.fetchval("SELECT 1")
            checks["services"]["postgres"] = "ok"
        except Exception:
            checks["services"]["postgres"] = "error"
    else:
        checks["services"]["postgres"] = "not_connected"

    # Embedding provider
    try:
        healthy = await embedding_provider.health_check(http_client)
        checks["services"]["embedding_provider"] = "ok" if healthy else "error"
    except Exception:
        checks["services"]["embedding_provider"] = "error"

    return checks


@app.post("/ingest")
async def ingest(req: IngestRequest):
    t0 = time.perf_counter()
    try:
        collection = req.collection or DEFAULT_COLLECTION
        chunks = chunk_text(req.source)
        source_type = req.source_type or "text"
        result = await ingest_text_chunks(
            chunks=chunks,
            collection=collection,
            source=f"{source_type}:inline",
            classification=req.classification,
            project=req.project,
            metadata=req.metadata,
            source_type=source_type,
        )
        pb_ingestion_requests.labels(endpoint="ingest", status="ok").inc() if pb_ingestion_requests else None
        return result
    except Exception:
        pb_ingestion_requests.labels(endpoint="ingest", status="error").inc() if pb_ingestion_requests else None
        raise
    finally:
        pb_ingestion_duration.labels(endpoint="ingest").observe(time.perf_counter() - t0) if pb_ingestion_duration else None


@app.post("/ingest/chunks")
async def ingest_chunks(req: ChunkIngestRequest):
    """Ingest pre-processed chunks from adapters. Full privacy pipeline applies."""
    t0 = time.perf_counter()
    try:
        result = await ingest_text_chunks(
            chunks=req.chunks,
            collection=req.collection,
            source=req.source,
            classification=req.classification,
            project=req.project,
            metadata=req.metadata,
            source_type=req.source_type,
        )
        pb_ingestion_requests.labels(endpoint="ingest_chunks", status="ok").inc() if pb_ingestion_requests else None
        return result
    except Exception:
        pb_ingestion_requests.labels(endpoint="ingest_chunks", status="error").inc() if pb_ingestion_requests else None
        raise
    finally:
        pb_ingestion_duration.labels(endpoint="ingest_chunks").observe(time.perf_counter() - t0) if pb_ingestion_duration else None


@app.post("/snapshots/create")
async def snapshot_create(req: SnapshotRequest):
    """Creates a knowledge snapshot (Qdrant + PG + OPA)."""
    try:
        result = await create_snapshot(
            name=req.name,
            description=req.description,
            created_by=req.created_by,
        )
        return result
    except Exception as e:
        log.error(f"Snapshot creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Repository Sync ─────────────────────────────────────────


@app.post("/sync")
async def sync_all():
    """Sync all configured Git repositories."""
    from ingestion.sync_service import sync_all_repos

    if not pg_pool:
        raise HTTPException(status_code=503, detail="Database not connected")

    results = await sync_all_repos(
        pool=pg_pool,
        http_client=http_client,
        ingestion_url=f"http://localhost:{os.getenv('PORT', '8081')}",
        qdrant_url=QDRANT_URL,
    )
    return {"repos": results}


@app.post("/sync/{repo_name}")
async def sync_single(repo_name: str):
    """Sync a single configured Git repository by name."""
    from ingestion.sync_service import load_repo_configs, sync_repo

    if not pg_pool:
        raise HTTPException(status_code=503, detail="Database not connected")

    configs = load_repo_configs()
    config = next((c for c in configs if c.name == repo_name), None)
    if not config:
        raise HTTPException(status_code=404, detail=f"Repo '{repo_name}' not found in repos.yaml")

    result = await sync_repo(
        config=config,
        pool=pg_pool,
        http_client=http_client,
        ingestion_url=f"http://localhost:{os.getenv('PORT', '8081')}",
        qdrant_url=QDRANT_URL,
    )
    return result
