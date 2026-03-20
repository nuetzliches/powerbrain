#!/usr/bin/env python3
"""Seed a minimal searchable demo document into Qdrant for MVP verification."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

DEMO_DOC_ID = "search-first-mvp-doc"
DEMO_CONTENT = "Search-first MVP smoke test document for the Wissensdatenbank MCP server."


def request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def ensure_collection(name: str) -> None:
    request_json(
        "PUT",
        f"{QDRANT_URL}/collections/{name}",
        {"vectors": {"size": 768, "distance": "Cosine"}},
    )


def embed_text(text: str) -> list[float]:
    response = request_json(
        "POST",
        f"{OLLAMA_URL}/api/embed",
        {"model": EMBEDDING_MODEL, "input": text},
    )
    return response["embeddings"][0]


def upsert_demo_document(vector: list[float]) -> None:
    payload = {
        "points": [
            {
                "id": DEMO_DOC_ID,
                "vector": vector,
                "payload": {
                    "content": DEMO_CONTENT,
                    "classification": "internal",
                    "source": "demo-seed",
                    "title": "Search-first MVP Demo",
                    "project": "power-brain",
                    "type": "doc",
                },
            }
        ]
    }
    request_json(
        "PUT",
        f"{QDRANT_URL}/collections/knowledge_general/points?wait=true",
        payload,
    )


def main() -> int:
    try:
        for collection in ("knowledge_general", "knowledge_code", "knowledge_rules"):
            ensure_collection(collection)

        vector = embed_text(DEMO_CONTENT)
        upsert_demo_document(vector)
    except urllib.error.URLError as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"seeded": True, "document_id": DEMO_DOC_ID}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
