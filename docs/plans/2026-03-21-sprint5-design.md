# Sprint 5 Design: P2-6 + P3-2 + P3-3

**Date:** 2026-03-21
**Scope:** Apache AGE Hardening, Rate Limiting, Ingestion Cleanup + Chunk API

---

## Overview

| Issue | Title | Type | Effort |
|-------|-------|------|--------|
| P2-6 | Apache AGE limitations | Bugfix + Hardening | Medium |
| P3-2 | No rate limiting | New feature | Medium |
| P3-3 | Ingestion pipeline stubs | Cleanup + API | Medium |

---

## P2-6: Apache AGE Hardening

### Problem

1. **Missing `graph_sync_log` table:** `_log_sync()` in `graph_service.py:333`
   writes to a table that does not exist in any migration. Every graph mutation
   crashes during logging.
2. **Fragile agtype parsing:** `json.loads(str(raw))` breaks on AGE-specific
   suffixes (`::vertex`, `::edge`).
3. **`shortestPath` bugs:** Known issues in AGE with directed graphs.
4. **Variable-depth traversal:** `[r*1..depth]` returns paths/lists instead of individual
   relationships.

### Design

**Migration `011_graph_sync_log.sql`:**
- Table with: `id SERIAL`, `operation TEXT`, `label TEXT`, `node_id TEXT`,
  `details JSONB`, `created_at TIMESTAMPTZ DEFAULT now()`

**`graph_service.py` hardening:**
- `_execute_cypher()`: Strip AGE agtype suffixes (`::vertex`, `::edge`, `::path`) via
  regex before `json.loads`. More robust fallback parsing.
- `find_path()`: Try/except around `shortestPath()`. On AGE error, fall back to
  iterative BFS via `get_neighbors()` with depth limit.
- Variable-depth: Parse result as a list when `*` is in the query.

**Tests:**
- Migration defines `graph_sync_log` table
- `_execute_cypher` has agtype suffix handling (regex pattern)
- `find_path` has fallback logic

---

## P3-2: Rate Limiting

### Problem

No rate limiting — an agent can flood the MCP server.

### Design

**In-memory token bucket per `agent_id`, limits per role via env vars.**

**Configuration:**
```
RATE_LIMIT_ANALYST=60       # Requests per minute
RATE_LIMIT_DEVELOPER=120
RATE_LIMIT_ADMIN=300
RATE_LIMIT_ENABLED=true     # Can be fully disabled
```

**Token bucket:**
- `TokenBucket` class with capacity, refill rate, asyncio lock
- Registry: `dict[str, TokenBucket]` — one bucket per `agent_id`
- Cleanup: Remove buckets after 10 min of inactivity

**Integration:**
- Starlette middleware after auth, before MCP dispatch
- HTTP 429 with `Retry-After` header on exceedance
- Prometheus counter `kb_rate_limit_rejected_total`
- `/health` and `/metrics` excluded

**Graceful degradation:**
- Rate limiter error → let requests through (fail-open)

**Tests:**
- Env var configuration is read
- Middleware present in the chain
- 429 response logic exists
- Fail-open pattern present

---

## P3-3: Ingestion Cleanup + Chunk API

### Problem

`/ingest` mixes source parsing with KB pipeline. `git_repo` and `sql_dump` are
stubs. Adapters (Forgejo, etc.) need a clean entry point.

### Architecture decision

`/ingest` is the endpoint via which agents write text into the KB.
Adapters (Forgejo, CSV imports, etc.) are separate components that preprocess
data and feed it in via an internal chunk endpoint.
Both paths go through the same privacy pipeline (`ingest_text_chunks()`).

```
Agent (MCP ingest_data) → POST /ingest       → ingest_text_chunks()
Adapter (internal)      → POST /ingest/chunks → ingest_text_chunks()
                                                  ↓
                                            PII → OPA → Vault → Embed → Qdrant
```

### Design

**Remove stubs:**
- Remove `git_repo` and `sql_dump` branches from `/ingest`
- MCP tool `ingest_data` schema: Reduce `source_type` enum to `["text"]`
- Clear error message on unknown `source_type`

**New endpoint `POST /ingest/chunks`:**
```python
class ChunkIngestRequest(BaseModel):
    chunks: list[str]                    # Preprocessed text chunks
    project: str
    collection: str = "knowledge_general"
    classification: str = "internal"
    metadata: dict = {}
    source: str = ""                     # Origin identifier
```
- Calls `ingest_text_chunks()` — full pipeline
- Only reachable from the Docker network (no external access)

**Simplify `/ingest`:**
- Only `text` as source type
- Chunking + `ingest_text_chunks()`
- No source-type switch

**Tests:**
- MCP tool schema only has `text` as source type
- `/ingest/chunks` endpoint exists
- ChunkIngestRequest model has expected fields

---

## Dependencies

The three tasks are independent of each other and can be implemented in parallel.
Task 4 (Docker rebuild + live verify) depends on all three.

## Open points for later sprints

- Forgejo adapter as the first adapter implementation
- Multimodal ingestion (images, videos)
- Redis-backed rate limiting for multi-instance
- AGE integration tests against a running instance
