#!/usr/bin/env python3
"""
Seed testdata into the Wissensdatenbank via the Ingestion API.

Reads documents from documents.json and sends each one through the
Ingestion API (POST /ingest). The API handles embedding (via Ollama),
PII scanning, OPA policy checks, and storage in Qdrant + PostgreSQL.

Usage:
    python3 seed.py                    # uses defaults
    INGESTION_URL=http://... python3 seed.py

Environment variables:
    INGESTION_URL  — Ingestion API base URL (default: http://ingestion:8081)
    OLLAMA_URL     — Ollama API base URL (default: http://ollama:11434)
    QDRANT_URL     — Qdrant API base URL (default: http://qdrant:6333)
    MCP_URL        — MCP server base URL (default: http://mcp-server:8080)
    EMBEDDING_MODEL — Ollama model name (default: nomic-embed-text)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

INGESTION_URL = os.environ.get("INGESTION_URL", "http://ingestion:8081")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
MCP_URL = os.environ.get("MCP_URL", "http://mcp-server:8080")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "120"))
POLL_INTERVAL = 3

DOCUMENTS_FILE = Path(__file__).parent / "documents.json"


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def http_get(url: str, timeout: int = 10) -> dict | str | None:
    """GET request, returns parsed JSON (dict) or raw text on success, None on failure."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            try:
                return json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return body  # plain text response (e.g. Qdrant healthz)
    except Exception:
        return None


def http_post(url: str, payload: dict, timeout: int = 120) -> dict:
    """POST request with JSON body, returns parsed JSON."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


def http_put(url: str, payload: dict, timeout: int = 30) -> dict:
    """PUT request with JSON body, returns parsed JSON."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return {}  # already exists
        raise


# ── Wait for services ────────────────────────────────────────────────────────


def wait_for_service(name: str, url: str) -> None:
    """Poll a URL until it returns 2xx, or give up after MAX_WAIT_SECONDS."""
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        result = http_get(url)
        if result is not None:
            print(f"  {name}: ready")
            return
        time.sleep(POLL_INTERVAL)
    print(f"  {name}: TIMEOUT after {MAX_WAIT_SECONDS}s — aborting", file=sys.stderr)
    sys.exit(1)


def wait_for_all_services() -> None:
    """Wait for all required services to be healthy."""
    print("Waiting for services...")
    wait_for_service("Qdrant", f"{QDRANT_URL}/healthz")
    wait_for_service("Ollama", f"{OLLAMA_URL}/api/tags")
    wait_for_service("Ingestion API", f"{INGESTION_URL}/health")
    # MCP server has no /health — try initialize
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        try:
            http_post(f"{MCP_URL}/mcp", {
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "seed", "version": "1.0"},
                },
            })
            print("  MCP server: ready")
            break
        except Exception:
            time.sleep(POLL_INTERVAL)
    else:
        print("  MCP server: TIMEOUT — aborting", file=sys.stderr)
        sys.exit(1)
    print()


# ── Ollama model ─────────────────────────────────────────────────────────────


def ensure_ollama_model() -> None:
    """Pull the embedding model if not already present (Ollama only)."""
    # Model pull is an Ollama-specific operation.
    # Skip if using a non-Ollama embedding provider.
    embedding_url = os.environ.get("EMBEDDING_PROVIDER_URL", "")
    if embedding_url and embedding_url != OLLAMA_URL:
        print(f"Skipping Ollama model pull (using external provider: {embedding_url})")
        return

    tags = http_get(f"{OLLAMA_URL}/api/tags")
    if tags:
        models = [m.get("name", "").split(":")[0] for m in tags.get("models", [])]
        if EMBEDDING_MODEL in models:
            print(f"Ollama model '{EMBEDDING_MODEL}' already loaded.")
            return

    print(f"Pulling Ollama model '{EMBEDDING_MODEL}'... (this may take a while)")
    try:
        http_post(f"{OLLAMA_URL}/api/pull", {"name": EMBEDDING_MODEL}, timeout=600)
        print(f"  Model '{EMBEDDING_MODEL}' pulled successfully.")
    except Exception as exc:
        print(f"  WARNING: Could not pull model: {exc}", file=sys.stderr)
        print("  Continuing — model may already be available.", file=sys.stderr)
    print()


# ── Qdrant collections ──────────────────────────────────────────────────────


def ensure_collections(collections: set[str]) -> None:
    """Create Qdrant collections if they don't exist."""
    print("Ensuring Qdrant collections...")
    for name in sorted(collections):
        http_put(f"{QDRANT_URL}/collections/{name}", {
            "vectors": {"size": 768, "distance": "Cosine"},
        })
        print(f"  {name}: ready")
    print()


# ── Ingestion ────────────────────────────────────────────────────────────────


def ingest_document(doc: dict) -> dict:
    """Send a single document through the Ingestion API."""
    payload = {
        "source": doc["content"],
        "source_type": "json",
        "collection": doc["collection"],
        "project": doc.get("project"),
        "classification": doc["classification"],
        "metadata": {
            "title": doc["title"],
            "original_source": doc["source"],
            "type": doc["type"],
            "seed_id": doc["id"],
        },
    }
    return http_post(f"{INGESTION_URL}/ingest", payload)


# ── Verification ─────────────────────────────────────────────────────────────


def verify_seeded_data() -> bool:
    """Quick verification: search via MCP and check we get results."""
    print("Verifying seeded data via MCP search...")

    # Initialize MCP session
    http_post(f"{MCP_URL}/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "seed-verify", "version": "1.0"},
        },
    })

    # Search each collection
    collections = {
        "pb_general": "NovaTech",
        "pb_code": "Coding Standards",
        "pb_rules": "Datenklassifizierung",
    }

    all_ok = True
    for collection, query in collections.items():
        result = http_post(f"{MCP_URL}/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "search_knowledge",
                "arguments": {
                    "query": query,
                    "collection": collection,
                    "top_k": 1,
                    "agent_id": "seed-verify",
                    "agent_role": "admin",
                },
            },
        })

        try:
            content = json.loads(result["result"]["content"][0]["text"])
            total = content.get("total", 0)
            if total > 0:
                print(f"  {collection}: {total} result(s) found")
            else:
                print(f"  {collection}: WARNING — no results found", file=sys.stderr)
                all_ok = False
        except Exception as exc:
            print(f"  {collection}: ERROR — {exc}", file=sys.stderr)
            all_ok = False

    return all_ok


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    # Load documents
    if not DOCUMENTS_FILE.exists():
        print(f"ERROR: {DOCUMENTS_FILE} not found", file=sys.stderr)
        return 1

    with open(DOCUMENTS_FILE) as f:
        documents: list[dict] = json.load(f)

    print(f"Testdata Seed: {len(documents)} documents")
    print(f"  Ingestion API: {INGESTION_URL}")
    print(f"  Ollama:        {OLLAMA_URL}")
    print(f"  Qdrant:        {QDRANT_URL}")
    print(f"  MCP:           {MCP_URL}")
    print()

    # 1. Wait for services
    wait_for_all_services()

    # 2. Ensure Ollama model
    ensure_ollama_model()

    # 3. Ensure Qdrant collections
    collections = {doc["collection"] for doc in documents}
    ensure_collections(collections)

    # 4. Ingest documents
    print(f"Ingesting {len(documents)} documents via Ingestion API...")
    t_start = time.time()
    errors: list[tuple[str, str]] = []
    ingested = 0

    for i, doc in enumerate(documents, 1):
        doc_id = doc["id"]
        try:
            result = ingest_document(doc)
            status = result.get("status", "unknown")
            chunks = result.get("chunks_ingested", 0)
            elapsed = time.time() - t_start

            if status == "ok":
                ingested += 1
                print(
                    f"  [{i:2d}/{len(documents)}] {doc['collection']:20s} | "
                    f"{doc['classification']:12s} | {doc['title'][:45]:45s} | "
                    f"{chunks} chunk(s) | {elapsed:.1f}s"
                )
            else:
                errors.append((doc_id, f"status={status}: {result}"))
                print(f"  [{i:2d}/{len(documents)}] WARN: {doc_id} — {result}", file=sys.stderr)

        except Exception as exc:
            errors.append((doc_id, str(exc)))
            print(f"  [{i:2d}/{len(documents)}] FAIL: {doc_id} — {exc}", file=sys.stderr)

    elapsed_total = time.time() - t_start
    print()
    print(f"Ingested {ingested}/{len(documents)} documents in {elapsed_total:.1f}s")

    if errors:
        print(f"\n{len(errors)} error(s):", file=sys.stderr)
        for doc_id, err in errors:
            print(f"  - {doc_id}: {err}", file=sys.stderr)

    # 5. Verify
    print()
    ok = verify_seeded_data()

    # Summary
    print()
    by_collection: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    for doc in documents:
        by_collection[doc["collection"]] = by_collection.get(doc["collection"], 0) + 1
        by_classification[doc["classification"]] = by_classification.get(doc["classification"], 0) + 1

    print("Per Collection:")
    for coll, count in sorted(by_collection.items()):
        print(f"  {coll}: {count}")
    print("Per Classification:")
    for cls, count in sorted(by_classification.items()):
        print(f"  {cls}: {count}")

    if errors:
        return 1
    if not ok:
        print("\nWARNING: Verification found issues — data may be incomplete.", file=sys.stderr)
        return 1

    print("\nSeed completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
