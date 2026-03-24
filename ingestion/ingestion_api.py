"""
Ingestion API – FastAPI-Wrapper
================================
HTTP-Schnittstelle für die Ingestion-Pipeline.
Wird vom MCP-Server über das Docker-Netzwerk aufgerufen.

Endpoints:
  POST /scan              — Text auf PII scannen (ohne Ingestion)
  POST /pseudonymize      — Text pseudonymisieren ohne Speicherung (Chat-Pfad)
  POST /ingest            — Text einspeisen (full privacy pipeline)
  POST /ingest/chunks     — Vorverarbeitete Chunks einspeisen (Adapter, ingest_text_chunks pipeline)
  POST /snapshots/create  — Wissens-Snapshot erstellen
  GET  /health            — Healthcheck
"""

import os
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import secrets

import httpx
import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

from pii_scanner import get_scanner
from snapshot_service import create_snapshot

# ── Konfiguration ────────────────────────────────────────────
QDRANT_URL   = os.getenv("QDRANT_URL",   "http://qdrant:6333")
POSTGRES_URL = os.getenv("POSTGRES_URL",  "postgresql://pb_admin:changeme@postgres:5432/powerbrain")
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

embedding_provider = EmbeddingProvider(
    base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY
)

# ── LLM / Layer generation provider ──
LLM_PROVIDER_URL = os.getenv("LLM_PROVIDER_URL", os.getenv("OLLAMA_URL", "http://pb-ollama:11434"))
LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b"))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LAYER_GENERATION_ENABLED = os.getenv("LAYER_GENERATION_ENABLED", "true").lower() == "true"

completion_provider = CompletionProvider(
    base_url=LLM_PROVIDER_URL, api_key=LLM_API_KEY
)

DEFAULT_COLLECTION = "pb_general"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pb-ingestion")

# ── FastAPI App ──────────────────────────────────────────────
app = FastAPI(title="Powerbrain Ingestion API", version="1.0.0")

# ── Clients (lifecycle-managed) ─────────────────────────────
qdrant: AsyncQdrantClient | None = None
http_client: httpx.AsyncClient | None = None
pg_pool: asyncpg.Pool | None = None


@app.on_event("startup")
async def startup():
    global qdrant, http_client, pg_pool
    qdrant = AsyncQdrantClient(url=QDRANT_URL)
    http_client = httpx.AsyncClient(timeout=60.0)
    try:
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
        log.info("PostgreSQL-Pool initialisiert")
    except Exception as e:
        log.error(f"PostgreSQL-Verbindung fehlgeschlagen: {e}")
        pg_pool = None


@app.on_event("shutdown")
async def shutdown():
    if pg_pool:
        await pg_pool.close()
    if http_client:
        await http_client.aclose()


# ── Request/Response-Modelle ────────────────────────────────

class IngestRequest(BaseModel):
    source: str
    source_type: str | None = "text"
    collection: str | None = None
    project: str | None = None
    classification: str = "internal"
    metadata: dict[str, Any] = {}


class SnapshotRequest(BaseModel):
    name: str = Field(description="Name des Snapshots")
    description: str = Field(default="", description="Beschreibung")
    created_by: str = Field(default="system", description="Erstellt von")


class ScanRequest(BaseModel):
    text: str = Field(min_length=1, description="Text der auf PII gescannt werden soll")
    language: str = Field(default="de", description="Sprache des Textes (de, en)")


class ScanResponse(BaseModel):
    contains_pii: bool = Field(description="Ob PII erkannt wurde")
    masked_text: str = Field(description="Text mit maskierten PII-Entitäten")
    entity_types: list[str] = Field(description="Liste erkannter PII-Typen")


class PseudonymizeRequest(BaseModel):
    text: str = Field(min_length=1, description="Text der pseudonymisiert werden soll")
    salt: str = Field(min_length=1, description="Salt für deterministische Pseudonyme")
    language: str = Field(default="de", description="Sprache des Textes (de, en)")


class PseudonymizeResponse(BaseModel):
    text: str = Field(description="Pseudonymisierter Text")
    mapping: dict[str, str] = Field(description="Mapping original → pseudonym")
    contains_pii: bool = Field(description="Ob PII erkannt wurde")
    entity_types: list[str] = Field(description="Liste erkannter PII-Typen")


class ChunkIngestRequest(BaseModel):
    """Request for adapter-based chunk ingestion. Internal use only."""
    chunks: list[str]
    project: str
    collection: str = "pb_general"
    classification: str = "internal"
    metadata: dict[str, Any] = {}
    source: str


# ── Hilfsfunktionen ─────────────────────────────────────────

async def get_embedding(text: str) -> list[float]:
    """Erzeugt Embedding über den konfigurierten Provider (OpenAI-compat)."""
    return await embedding_provider.embed(http_client, text, EMBEDDING_MODEL)


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 200) -> list[str]:
    """Einfaches Chunking mit Overlap für lange Texte."""
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
    """Holt oder erstellt einen Salt für das Projekt aus pii_vault.project_salts."""
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


async def check_opa_privacy(
    classification: str, contains_pii: bool, legal_basis: str | None = None
) -> dict:
    """Fragt OPA nach pii_action und dual_storage_enabled.

    OPA endpoint: /v1/data/pb/privacy/pii_action, /v1/data/pb/privacy/dual_storage_enabled
    """
    input_data = {
        "classification": classification,
        "contains_pii": contains_pii,
        "legal_basis": legal_basis or "",
    }
    result = {"pii_action": "block", "dual_storage_enabled": False}
    try:
        resp = await http_client.post(
            f"{OPA_URL}/v1/data/pb/privacy",
            json={"input": input_data},
        )
        resp.raise_for_status()
        data = resp.json().get("result", {})
        result["pii_action"] = data.get("pii_action", "block")
        result["dual_storage_enabled"] = data.get("dual_storage_enabled", False)
        result["retention_days"] = data.get("retention_days", 365)
    except Exception as e:
        log.warning(f"OPA privacy check failed, defaulting to block: {e}")
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
    """Speichert Originaltext + Mapping im pii_vault. Gibt vault_ref UUID zurück."""
    if not pg_pool:
        raise RuntimeError("PostgreSQL unavailable for vault storage")

    vault_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            # Original speichern
            await conn.execute("""
                INSERT INTO pii_vault.original_content
                    (id, document_id, chunk_index, original_text,
                     pii_entities, retention_expires_at, data_category)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, vault_id, doc_id, chunk_index, original_text,
                json.dumps(pii_entities), expires_at, data_category)

            # Mapping speichern (ein Eintrag pro Entity)
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
    """Schreibt einen Eintrag in pii_scan_log."""
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
) -> dict:
    """Vektorisiert Chunks und speichert sie in Qdrant + PostgreSQL.

    Pipeline:
    1. PII-Scan jedes Chunks
    2. OPA-Policy: pii_action + dual_storage_enabled (einmal pro Dokument)
    3. Je nach Action: mask, pseudonymize+vault, oder block
    4. Embed + Qdrant upsert
    5. PostgreSQL Metadaten
    """
    scanner = get_scanner()
    points = []
    pii_detected = False
    vault_refs: list[str | None] = []
    doc_id = str(uuid.uuid4())

    # Vorab documents_meta anlegen (damit Vault-FK-Constraints erfüllt sind)
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
                0,  # chunk_count wird später aktualisiert
                False,  # contains_pii wird später aktualisiert
                json.dumps(metadata),
            )
        except Exception as e:
            log.error(f"PG documents_meta Insert fehlgeschlagen: {e}")

    # OPA-Policy einmal pro Dokument abfragen (Klassifizierung ist gleich für alle Chunks)
    opa_result: dict | None = None

    for i, chunk in enumerate(chunks):
        # 1. PII-Scan
        scan_result = scanner.scan_text(chunk)
        vault_ref = None

        if scan_result.contains_pii:
            pii_detected = True

            # 2. OPA Policy: Was tun mit PII? (nur beim ersten Fund abfragen)
            if opa_result is None:
                opa_result = await check_opa_privacy(
                    classification, True, metadata.get("legal_basis")
                )
            pii_action = opa_result["pii_action"]
            dual_storage = opa_result["dual_storage_enabled"]
            retention_days = opa_result.get("retention_days", 365)

            if pii_action == "block":
                log.warning(
                    f"PII in Chunk {i} erkannt, Klassifizierung '{classification}'"
                    f" → blockiert durch OPA-Policy"
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
                # 3a. Dual Storage: pseudonymisieren + Original im Vault
                log.info(
                    f"PII in Chunk {i}: {scan_result.entity_counts}"
                    f" → pseudonymisiere (dual storage, action={pii_action})"
                )
                salt = await get_or_create_project_salt(project)
                pseudo_text, mapping = scanner.pseudonymize_text(chunk, salt)

                # Vault: Original + Mapping speichern
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
                # 3b. Fallback: maskieren (public oder dual_storage=false)
                if pii_action not in ("mask", "pseudonymize"):
                    log.warning(
                        f"PII action '{pii_action}' not fully implemented, "
                        f"falling back to mask"
                    )
                log.warning(
                    f"PII in Chunk {i}: {scan_result.entity_counts} → maskiere"
                )
                chunk = scanner.mask_text(chunk)
                await log_pii_scan(
                    source, scan_result.entity_counts, "mask", classification
                )

        # 4. Embedding
        embedding = await get_embedding(chunk)

        point_id = str(uuid.uuid4())
        payload = {
            "text": chunk,
            "source": source,
            "classification": classification,
            "project": project or "",
            "chunk_index": i,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "contains_pii": scan_result.contains_pii,
            "vault_ref": vault_ref,
            "layer": "L2",
            "doc_id": doc_id,
            **metadata,
        }
        points.append(PointStruct(
            id=point_id, vector=embedding, payload=payload,
        ))
        vault_refs.append(vault_ref)

    # 5. In Qdrant upserten (L2 chunks)
    if points:
        await qdrant.upsert(collection_name=collection, points=points)
        log.info(f"{len(points)} L2 Punkte in '{collection}' eingefügt")

    # 6. Generate L0/L1 layers and upsert as separate Qdrant points
    l0_point_id: str | None = None
    l1_point_id: str | None = None

    if points:
        # Collect the final (possibly pseudonymized/masked) chunk texts for LLM input
        processed_chunks = [p.payload["text"] for p in points]
        now_iso = datetime.now(timezone.utc).isoformat()

        # L0: Abstract
        l0_text = await generate_l0(processed_chunks, source=source, classification=classification)
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
        l1_text = await generate_l1(processed_chunks, source=source, classification=classification)
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

    # 7. documents_meta aktualisieren mit finalen Werten
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
            log.error(f"PG documents_meta Update fehlgeschlagen: {e}")

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
    """Scannt Text auf PII ohne Ingestion.

    Wird vom MCP-Server aufgerufen, bevor Audit-Log-Einträge
    mit Query-Text geschrieben werden.
    """
    scanner = get_scanner()
    scan_result = scanner.scan_text(req.text, language=req.language)

    if scan_result.contains_pii:
        masked = scanner.mask_text(req.text, language=req.language)
        entity_types = list(scan_result.entity_counts.keys())
    else:
        masked = req.text
        entity_types = []

    return ScanResponse(
        contains_pii=scan_result.contains_pii,
        masked_text=masked,
        entity_types=entity_types,
    )


@app.post("/pseudonymize")
async def pseudonymize(req: PseudonymizeRequest) -> PseudonymizeResponse:
    """Pseudonymisiert PII im Text ohne Speicherung.

    Wird vom pb-proxy aufgerufen, bevor Chat-Nachrichten
    an den LLM-Provider gesendet werden.
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
    collection = req.collection or DEFAULT_COLLECTION
    chunks = chunk_text(req.source)
    result = await ingest_text_chunks(
        chunks=chunks,
        collection=collection,
        source=f"{req.source_type or 'text'}:inline",
        classification=req.classification,
        project=req.project,
        metadata=req.metadata,
    )
    return result


@app.post("/ingest/chunks")
async def ingest_chunks(req: ChunkIngestRequest):
    """Ingest pre-processed chunks from adapters. Full privacy pipeline applies."""
    result = await ingest_text_chunks(
        chunks=req.chunks,
        collection=req.collection,
        source=req.source,
        classification=req.classification,
        project=req.project,
        metadata=req.metadata,
    )
    return result


@app.post("/snapshots/create")
async def snapshot_create(req: SnapshotRequest):
    """Erstellt einen Wissens-Snapshot (Qdrant + PG + OPA)."""
    try:
        result = await create_snapshot(
            name=req.name,
            description=req.description,
            created_by=req.created_by,
        )
        return result
    except Exception as e:
        log.error(f"Snapshot-Erstellung fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))
