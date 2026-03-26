# T1 Production Hardening Design

**Date:** 2026-03-25
**Status:** Draft
**Goal:** Reduce search latency by ~50% and make infrastructure configurable through 5 quick wins — no new services, no breaking changes.

## Context

Powerbrain's hot path (`search_knowledge`) currently takes 2.5–3.6s per request. The main bottlenecks are:

1. **Embedding** (300–500ms): Every query embeds from scratch — no caching, no batch support. Identical queries re-embed every time.
2. **OPA policy filtering** (50–150ms): Already parallelized, but identical `(role, classification, action)` tuples hit OPA repeatedly within and across requests.
3. **DB pool sizes** hardcoded — no env var tuning without code changes.
4. **Health checks** missing for mcp-server, ingestion, ollama — Docker can't restart failed containers.

`docs/scalability.md` documents these bottlenecks and defines a T1–T4 priority scheme. This spec covers T1 only.

## Constraints

- No new Docker services (no Redis, no PgBouncer, no Valkey)
- No breaking API changes
- All configuration via environment variables with backward-compatible defaults
- Cache abstraction must allow future swap to external store (Valkey)

## Design

### 1. Embedding Cache (`shared/embedding_cache.py`)

**What:** In-process TTL cache for embedding vectors. Sits between callers and `EmbeddingProvider.embed()`.

**Interface:**

```python
class EmbeddingCache:
    def __init__(self, maxsize: int, ttl: int):
        """maxsize: max entries, ttl: seconds."""

    def get(self, text: str, model: str) -> list[float] | None: ...
    def set(self, text: str, model: str, vector: list[float]) -> None: ...
    def stats(self) -> dict[str, int]:
        """Returns {"hits": N, "misses": N, "size": N}."""
```

**Key generation:** SHA-256 of `f"{model}:{text}"`. This is deterministic and avoids storing the full text in the cache key.

**Backend:** `cachetools.TTLCache` (already used pattern in the codebase via tenacity). Thread-safe with a simple `threading.Lock` (async code accesses it from the event loop thread only, but lock protects against concurrent coroutines sharing the cache dict).

**Configuration:**

| Env var | Default | Description |
|---------|---------|-------------|
| `EMBEDDING_CACHE_SIZE` | `2048` | Max cached embeddings |
| `EMBEDDING_CACHE_TTL` | `3600` | Cache TTL in seconds |
| `EMBEDDING_CACHE_ENABLED` | `true` | Kill switch |

**Integration point:** `EmbeddingProvider.embed()` is wrapped — callers (`embed_text()` in server.py, `get_embedding()` in ingestion_api.py) get caching transparently.

**Metrics:** Two Prometheus counters: `pb_embedding_cache_hits_total`, `pb_embedding_cache_misses_total`. Registered where Prometheus is available (mcp-server). Ingestion service uses the cache but skips metrics (no Prometheus there currently).

**Why not in `EmbeddingProvider` directly?** The provider is a thin HTTP wrapper in `shared/`. Coupling cache state into it would complicate testing and make the provider harder to reason about. A separate cache module keeps concerns clean and makes the Valkey swap trivial later.

**Invalidation:** TTL-based only. Model changes require a service restart (new `EMBEDDING_MODEL` env var), which clears the in-process cache. Explicit invalidation API is not needed at T1 — Valkey would bring `FLUSHDB` if needed later.

### 2. Embedding Batch API (`shared/llm_provider.py`)

**What:** New method `EmbeddingProvider.embed_batch(texts, model) -> list[list[float]]` that sends all texts in a single `/v1/embeddings` request.

**Implementation:**

```python
async def embed_batch(
    self, http: httpx.AsyncClient, texts: list[str], model: str
) -> list[list[float]]:
    resp = await http.post(
        f"{self.base_url}/v1/embeddings",
        headers=self.headers,
        json={"model": model, "input": texts},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    # OpenAI API returns results sorted by index
    data.sort(key=lambda x: x["index"])
    return [d["embedding"] for d in data]
```

The OpenAI `/v1/embeddings` spec accepts `input: string | list[string]`. Ollama, vLLM, and HF TEI all support this. No fallback needed — if the backend doesn't support batch, it will error and the caller can fall back to sequential.

**Cache integration:** The ingestion pipeline calls `embed_batch`, but checks the cache first per-item. Only cache misses go to the batch call. Results are stored back individually.

**Where used:**
- `ingestion/ingestion_api.py` `ingest_text_chunks()` — currently embeds chunks one by one in a loop. Will switch to batch.
- `mcp-server/server.py` `embed_text()` — single query embedding, keeps using `embed()` (single text, cache handles it).

### 3. OPA Result Cache (`mcp-server/server.py`)

**What:** TTL cache for `check_opa_policy()` results. OPA access decisions are deterministic for a given `(agent_role, classification, action)` tuple — the `agent_id` and `resource` don't affect the decision (OPA policy `pb.access.allow` only checks role + classification + action).

**Prior art:** `evaluation/run_eval.py` uses a simple unbounded `dict` cache for OPA results (`_opa_access_cache`). This works for single-run evaluation scripts but is unsuitable for a long-running server — no TTL means policy changes are never picked up. The MCP server implementation uses `TTLCache` instead.

**Cache key:** `(agent_role, classification, action)` — this is the minimal set that determines the OPA result. There are only ~12 possible combinations (3 roles x 4 classifications), so the cache is tiny.

**Backend:** `cachetools.TTLCache(maxsize=64, ttl=OPA_CACHE_TTL)`.

**Configuration:**

| Env var | Default | Description |
|---------|---------|-------------|
| `OPA_CACHE_TTL` | `60` | Cache TTL in seconds |
| `OPA_CACHE_ENABLED` | `true` | Kill switch |

**What is NOT cached:** Vault policies (`vault_access_allowed`, `vault_fields_to_redact`), summarization policies (`pb.summarization.*`), and proxy policies — these have additional input dimensions (purpose, token, etc.) and are called less frequently.

**Metrics:** `pb_opa_cache_hits_total`, `pb_opa_cache_misses_total`.

**Impact:** In a `search_knowledge` call with 50 oversampled results, all hits with the same classification resolve from cache after the first OPA call. Typical reduction: 50 OPA calls -> 1–4 OPA calls (one per unique classification in the result set).

### 4. Pool Size Environment Variables

**What:** Replace hardcoded `min_size` / `max_size` in `asyncpg.create_pool()` calls with environment variables.

**Configuration:**

| Env var | Default | Description |
|---------|---------|-------------|
| `PG_POOL_MIN` | `2` | Minimum pool connections |
| `PG_POOL_MAX` | `10` | Maximum pool connections |

**Affected files:**

| File | Current | After |
|------|---------|-------|
| `mcp-server/server.py:1719` | `min_size=2, max_size=10` | `min_size=PG_POOL_MIN, max_size=PG_POOL_MAX` |
| `ingestion/ingestion_api.py:89` | `min_size=2, max_size=10` | `min_size=PG_POOL_MIN, max_size=PG_POOL_MAX` |
| `pb-proxy/auth.py:34-42` | `min_size=1, max_size=5` | `min_size=PG_POOL_MIN, max_size=PG_POOL_MAX` |
| `ingestion/retention_cleanup.py` | `min_size=1, max_size=5` | `min_size=PG_POOL_MIN, max_size=PG_POOL_MAX` |

Default values match current behavior. Read via `shared/config.py` alongside existing `read_secret()` and `build_postgres_url()`.

### 5. Missing Docker Compose Health Checks

**What:** Add health checks for the 3 services that currently lack them.

```yaml
# mcp-server
healthcheck:
  test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]
  interval: 10s
  timeout: 5s
  retries: 3

# ingestion
healthcheck:
  test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8081/health')"]
  interval: 10s
  timeout: 5s
  retries: 3

# ollama
healthcheck:
  test: ["CMD", "curl", "-sf", "http://localhost:11434/api/tags"]
  interval: 30s
  timeout: 10s
  retries: 3
```

Ollama uses `curl` because it's available in the Ollama image. Python services use `urllib.request` (no curl in slim images). Ollama gets a longer interval (30s) because model loading can be slow.

**Health endpoint:** mcp-server's `RateLimitMiddleware` already skips `/health` (server.py:236), but no actual Starlette route serves that path — requests fall through to 404. Add a minimal health route to the Starlette `routes` list that returns 200. Ingestion already has `GET /health` via FastAPI.

## Expected Impact

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| Search latency (repeat query) | 2.5–3.6s | ~0.8–1.5s | 2–3x |
| OPA calls per search | 50 | 1–4 | 12–50x |
| Ingestion embedding throughput | 1 chunk/request | N chunks/request | 5–10x |
| DB pool configurability | Code change | Env var | Operational |

## Testing Strategy

- **Embedding cache:** Unit tests for `EmbeddingCache` (get/set/eviction/stats). Integration: mock `EmbeddingProvider`, verify cache hit avoids HTTP call.
- **Batch API:** Unit test for `embed_batch` with mocked HTTP. Verify index sorting. Verify cache integration (partial hits).
- **OPA cache:** Unit test for cache hit/miss/TTL expiry. Integration: mock OPA, verify call count reduction.
- **Pool env vars:** Verify env var reading in `shared/config.py` tests.
- **Health checks:** Covered by existing E2E smoke tests (they wait for health).

## Files Changed

| Action | File | Change |
|--------|------|--------|
| Create | `shared/embedding_cache.py` | Cache class with TTL backend |
| Modify | `shared/llm_provider.py` | Add `embed_batch()` method |
| Modify | `shared/config.py` | Add `PG_POOL_MIN`/`PG_POOL_MAX` readers |
| Modify | `mcp-server/server.py` | Wire embedding cache, OPA cache, pool env vars, add `/health` route |
| Modify | `ingestion/ingestion_api.py` | Wire embedding cache, batch embedding, pool env vars |
| Modify | `pb-proxy/auth.py` | Pool env vars |
| Modify | `ingestion/retention_cleanup.py` | Pool env vars |
| Modify | `docker-compose.yml` | Health checks, new env vars |
| Modify | `.env.example` | Document new env vars |
| Create | `shared/tests/test_embedding_cache.py` | Cache unit tests |
| Modify | `shared/tests/test_llm_provider.py` | Batch API tests |
| Create | `mcp-server/tests/test_opa_cache.py` | OPA cache tests |
| Modify | `CLAUDE.md` | Update architecture docs |
