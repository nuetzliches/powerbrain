"""
Ingestion API – FastAPI-Wrapper
================================
HTTP-Schnittstelle für die Ingestion-Pipeline.
Wird vom MCP-Server über das Docker-Netzwerk aufgerufen.

Endpoints:
  POST /ingest            — Daten einspeisen (CSV, JSON, SQL-Dump, Git-Repo)
  POST /snapshots/create  — Wissens-Snapshot erstellen
  GET  /health            — Healthcheck
"""

import os
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

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
POSTGRES_URL = os.getenv("POSTGRES_URL",  "postgresql://kb_admin:changeme@postgres:5432/knowledgebase")
OPA_URL      = os.getenv("OPA_URL",       "http://opa:8181")
OLLAMA_URL   = os.getenv("OLLAMA_URL",    "http://ollama:11434")
RERANKER_URL = os.getenv("RERANKER_URL",  "http://reranker:8082")

EMBEDDING_MODEL = "nomic-embed-text"

# Collection-Mapping: source_type → Qdrant-Collection
COLLECTION_MAP = {
    "csv":      "knowledge_general",
    "json":     "knowledge_general",
    "sql_dump": "knowledge_general",
    "git_repo": "knowledge_code",
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kb-ingestion")

# ── FastAPI App ──────────────────────────────────────────────
app = FastAPI(title="KB Ingestion API", version="1.0.0")

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
    source: str = Field(description="Quelle: Pfad, URL oder Inline-Daten")
    source_type: str = Field(description="csv, json, sql_dump, git_repo")
    project: str | None = Field(default=None, description="Projekt-Zuordnung")
    classification: str = Field(default="internal", description="Datenklassifizierung")
    metadata: dict[str, Any] = Field(default_factory=dict)


class SnapshotRequest(BaseModel):
    name: str = Field(description="Name des Snapshots")
    description: str = Field(default="", description="Beschreibung")
    created_by: str = Field(default="system", description="Erstellt von")


# ── Hilfsfunktionen ─────────────────────────────────────────

async def get_embedding(text: str) -> list[float]:
    """Erzeugt Embedding über Ollama."""
    resp = await http_client.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": text},
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


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
    import secrets
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
    return salt


async def check_opa_privacy(
    classification: str, contains_pii: bool, legal_basis: str | None = None
) -> dict:
    """Fragt OPA nach pii_action und dual_storage_enabled.

    OPA endpoint: /v1/data/kb/privacy/pii_action, /v1/data/kb/privacy/dual_storage_enabled
    """
    input_data = {
        "classification": classification,
        "contains_pii": contains_pii,
        "legal_basis": legal_basis or "",
    }
    result = {"pii_action": "block", "dual_storage_enabled": False}
    try:
        resp = await http_client.post(
            f"{OPA_URL}/v1/data/kb/privacy",
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
    2. OPA-Policy: pii_action + dual_storage_enabled
    3. Je nach Action: mask, pseudonymize+vault, oder block
    4. Embed + Qdrant upsert
    5. PostgreSQL Metadaten
    """
    scanner = get_scanner()
    points = []
    pii_detected = False
    vault_refs: list[str | None] = []
    doc_id = str(uuid.uuid4())

    for i, chunk in enumerate(chunks):
        # 1. PII-Scan
        scan_result = scanner.scan_text(chunk)
        vault_ref = None

        if scan_result.contains_pii:
            pii_detected = True

            # 2. OPA Policy: Was tun mit PII?
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

            elif pii_action == "pseudonymize" and dual_storage:
                # 3a. Dual Storage: pseudonymisieren + Original im Vault
                log.info(
                    f"PII in Chunk {i}: {scan_result.entity_counts}"
                    f" → pseudonymisiere (dual storage)"
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
            **metadata,
        }
        points.append(PointStruct(
            id=point_id, vector=embedding, payload=payload,
        ))
        vault_refs.append(vault_ref)

    # 5. In Qdrant upserten
    if points:
        await qdrant.upsert(collection_name=collection, points=points)
        log.info(f"{len(points)} Punkte in '{collection}' eingefügt")

    # 6. Metadaten in PostgreSQL speichern
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
                len(points),
                pii_detected,
                json.dumps({
                    **metadata,
                    "pii_detected": pii_detected,
                    "vault_refs": [v for v in vault_refs if v],
                }),
            )
        except Exception as e:
            log.error(f"PG documents_meta Insert fehlgeschlagen: {e}")

    return {
        "status": "ok",
        "collection": collection,
        "chunks_ingested": len(points),
        "pii_detected": pii_detected,
        "dual_storage": any(v is not None for v in vault_refs),
    }


# ── Endpoints ────────────────────────────────────────────────

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

    # Ollama
    try:
        await http_client.get(f"{OLLAMA_URL}/api/tags")
        checks["services"]["ollama"] = "ok"
    except Exception:
        checks["services"]["ollama"] = "error"

    return checks


@app.post("/ingest")
async def ingest(req: IngestRequest):
    """
    Speist Daten in die Wissensdatenbank ein.
    Aktuell unterstützt: Inline-Text über das 'source'-Feld.
    CSV/JSON/git_repo: Stub mit Platzhalter-Logik.
    """
    collection = COLLECTION_MAP.get(req.source_type, "knowledge_general")

    if req.source_type in ("csv", "json"):
        # Für CSV/JSON: source als Inline-Daten oder Pfad behandeln
        # Aktuell: source direkt als Text chunken (MVP)
        chunks = chunk_text(req.source)
        result = await ingest_text_chunks(
            chunks=chunks,
            collection=collection,
            source=f"{req.source_type}:inline",
            classification=req.classification,
            project=req.project,
            metadata=req.metadata,
        )
        return result

    elif req.source_type == "git_repo":
        # Git-Repo Ingestion: Platzhalter für spätere Implementierung
        # TODO: git clone → Dateien lesen → chunken → vektorisieren
        return {
            "status": "stub",
            "message": f"Git-Repo Ingestion für '{req.source}' noch nicht implementiert. "
                       "Wird in der nächsten Iteration hinzugefügt.",
            "collection": collection,
        }

    elif req.source_type == "sql_dump":
        # SQL-Dump Ingestion: Platzhalter
        return {
            "status": "stub",
            "message": f"SQL-Dump Ingestion für '{req.source}' noch nicht implementiert.",
            "collection": collection,
        }

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unbekannter source_type: '{req.source_type}'. "
                   f"Erlaubt: csv, json, sql_dump, git_repo",
        )


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
