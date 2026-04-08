# Testdata Seed Design

**Goal:** Track reproducible test data in the repo and seed it automatically via a Docker Compose profile.

**Architecture:** 21 NovaTech test documents are versioned as a JSON file in `testdata/`. A seed container reads the documents and sends them one by one through the Ingestion API (`POST /ingest`). Ollama generates the embeddings live. The seed container only runs with `docker compose --profile seed up`.

## Decisions

| Question | Decision | Reason |
|----------|----------|--------|
| Embeddings | Live via Ollama | Easier to maintain, no regeneration when the model changes |
| Seed path | Through Ingestion API | Exercises the real path (PII scan, OPA, PostgreSQL metadata) |
| Trigger | Docker Compose `--profile seed` | A single command for a complete test environment |
| Data format | JSON file in the repo | Reviewable, diffable, versioned |

## Changes

### 1. Ingestion API: `collection` override (ingestion/ingestion_api.py)

New optional `collection` field in `IngestRequest`. When set, the `COLLECTION_MAP` logic is skipped. Required because `source_type=json` would otherwise always map to `knowledge_general`, but the test data needs all 3 collections.

### 2. testdata/documents.json

21 documents extracted from `scripts/seed_comprehensive_testdata.py`:
- `knowledge_general`: 10 documents (HR, Finance, Culture, Legal, Ops)
- `knowledge_code`: 6 documents (Dev Guidelines, API, Security)
- `knowledge_rules`: 5 documents (Governance, Compliance)

Format per document:
```json
{
  "id": "novatech-onboarding-checklist",
  "collection": "knowledge_general",
  "classification": "public",
  "title": "Onboarding-Checkliste",
  "source": "hr-wiki",
  "project": "novatech-hr",
  "type": "doc",
  "content": "..."
}
```

### 3. testdata/seed.py

Python script (stdlib + urllib only):
1. Waits for Ollama, Ingestion API, MCP server (HTTP health polls)
2. Pulls the `nomic-embed-text` model if not present
3. Creates Qdrant collections if needed
4. Iterates over documents.json, calls `POST /ingest` for each document
5. Verifies via MCP `search_knowledge` that the data is retrievable

### 4. testdata/Dockerfile

Minimal Python image, copies seed.py + documents.json.

### 5. docker-compose.yml: Seed service

```yaml
seed:
  profiles: ["seed"]
  build: ./testdata
  depends_on:
    qdrant: { condition: service_healthy }
    postgres: { condition: service_healthy }
    opa: { condition: service_healthy }
    ingestion: { condition: service_started }
    ollama: { condition: service_started }
  environment:
    INGESTION_URL: http://ingestion:8081
    OLLAMA_URL: http://ollama:11434
    MCP_URL: http://mcp-server:8080
    QDRANT_URL: http://qdrant:6333
```

### 6. Cleanup: scripts/seed_comprehensive_testdata.py

Replaced by a reference to `testdata/`. The old script either remains as a deprecated reference or is removed.

## Usage

```bash
# Full stack with test data
docker compose --profile seed up -d

# Infrastructure only (without test data)
docker compose up -d
```
