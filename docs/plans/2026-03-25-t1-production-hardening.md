# T1 Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce search latency ~50% and make infrastructure configurable through 5 quick wins — no new services, no breaking changes.

**Architecture:** In-process TTL caches for embeddings and OPA results, batch embedding API for ingestion throughput, configurable PG pool sizes, and missing Docker health checks. All changes are additive with backward-compatible defaults.

**Tech Stack:** Python 3.12, cachetools, asyncpg, httpx, pytest, Docker Compose

---

## File Inventory

### Create
- `shared/embedding_cache.py` — Embedding cache with TTL backend
- `shared/tests/test_embedding_cache.py` — Cache unit tests
- `mcp-server/tests/test_opa_cache.py` — OPA cache tests

### Modify
- `shared/llm_provider.py` — Add `embed_batch()` method
- `shared/tests/test_llm_provider.py` — Batch API tests
- `shared/config.py` — Add pool size readers
- `mcp-server/server.py` — Wire caches, pool env vars, add `/health` route
- `mcp-server/requirements.txt` — Add `cachetools` dependency
- `ingestion/ingestion_api.py` — Wire embedding cache + batch, pool env vars
- `ingestion/requirements.txt` — Add `cachetools` dependency
- `pb-proxy/auth.py` — Pool env vars
- `ingestion/retention_cleanup.py` — Pool env vars
- `docker-compose.yml` — Health checks, new env vars
- `.env.example` — Document new env vars
- `CLAUDE.md` — Update docs

---

### Task 1: Embedding Cache (`shared/embedding_cache.py`)

**Files:**
- Create: `shared/embedding_cache.py`
- Create: `shared/tests/test_embedding_cache.py`
- Modify: `mcp-server/requirements.txt` — add `cachetools>=5.3`
- Modify: `ingestion/requirements.txt` — add `cachetools>=5.3`

- [ ] **Step 1: Add `cachetools` dependency**

Add `cachetools>=5.3` to both requirements files:

`mcp-server/requirements.txt` — append line:
```
cachetools>=5.3
```

`ingestion/requirements.txt` — append line:
```
cachetools>=5.3
```

- [ ] **Step 2: Write failing tests for EmbeddingCache**

Create `shared/tests/test_embedding_cache.py`:

```python
"""Tests for shared.embedding_cache module."""

from __future__ import annotations

import time

import pytest

from shared.embedding_cache import EmbeddingCache


class TestEmbeddingCacheGetSet:
    def test_miss_returns_none(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        assert cache.get("hello", "model-a") is None

    def test_set_then_get(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        vec = [0.1, 0.2, 0.3]
        cache.set("hello", "model-a", vec)
        assert cache.get("hello", "model-a") == vec

    def test_different_models_are_separate_keys(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        cache.set("hello", "model-a", [1.0])
        cache.set("hello", "model-b", [2.0])
        assert cache.get("hello", "model-a") == [1.0]
        assert cache.get("hello", "model-b") == [2.0]

    def test_different_texts_are_separate_keys(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        cache.set("hello", "m", [1.0])
        cache.set("world", "m", [2.0])
        assert cache.get("hello", "m") == [1.0]
        assert cache.get("world", "m") == [2.0]


class TestEmbeddingCacheEviction:
    def test_maxsize_eviction(self):
        cache = EmbeddingCache(maxsize=2, ttl=60)
        cache.set("a", "m", [1.0])
        cache.set("b", "m", [2.0])
        cache.set("c", "m", [3.0])
        # "a" should be evicted (LRU)
        assert cache.get("a", "m") is None
        assert cache.get("c", "m") == [3.0]

    def test_ttl_expiry(self):
        cache = EmbeddingCache(maxsize=10, ttl=1)
        cache.set("hello", "m", [1.0])
        assert cache.get("hello", "m") == [1.0]
        time.sleep(1.1)
        assert cache.get("hello", "m") is None


class TestEmbeddingCacheStats:
    def test_initial_stats(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        assert cache.stats() == {"hits": 0, "misses": 0, "size": 0}

    def test_hit_miss_counting(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        cache.set("a", "m", [1.0])
        cache.get("a", "m")       # hit
        cache.get("b", "m")       # miss
        cache.get("a", "m")       # hit
        stats = cache.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["size"] == 1


class TestEmbeddingCacheDisabled:
    def test_disabled_cache_returns_none(self):
        cache = EmbeddingCache(maxsize=10, ttl=60, enabled=False)
        cache.set("a", "m", [1.0])
        assert cache.get("a", "m") is None

    def test_disabled_cache_stats_empty(self):
        cache = EmbeddingCache(maxsize=10, ttl=60, enabled=False)
        cache.set("a", "m", [1.0])
        cache.get("a", "m")
        assert cache.stats() == {"hits": 0, "misses": 0, "size": 0}
```

- [ ] **Step 3: Run tests — verify they fail**

```bash
cd shared && python3 -m pytest tests/test_embedding_cache.py -v
```

Expected: `ModuleNotFoundError: No module named 'shared.embedding_cache'`

- [ ] **Step 4: Implement EmbeddingCache**

Create `shared/embedding_cache.py`:

```python
"""
In-process TTL cache for embedding vectors.

Sits between callers and EmbeddingProvider.embed(). Key is SHA-256
of "{model}:{text}" — deterministic and avoids storing raw text.

Backend: cachetools.TTLCache. Designed for future swap to Valkey
by replacing the get/set methods.

Configuration via env vars:
  EMBEDDING_CACHE_SIZE    (default: 2048)
  EMBEDDING_CACHE_TTL     (default: 3600)
  EMBEDDING_CACHE_ENABLED (default: true)
"""

from __future__ import annotations

import hashlib
import os
import threading

from cachetools import TTLCache


class EmbeddingCache:
    """Thread-safe TTL cache for embedding vectors."""

    def __init__(
        self,
        maxsize: int | None = None,
        ttl: int | None = None,
        enabled: bool | None = None,
    ):
        if maxsize is None:
            maxsize = int(os.getenv("EMBEDDING_CACHE_SIZE", "2048"))
        if ttl is None:
            ttl = int(os.getenv("EMBEDDING_CACHE_TTL", "3600"))
        if enabled is None:
            enabled = os.getenv("EMBEDDING_CACHE_ENABLED", "true").lower() == "true"

        self._enabled = enabled
        self._cache: TTLCache[str, list[float]] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(text: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{text}".encode()).hexdigest()

    def get(self, text: str, model: str) -> list[float] | None:
        if not self._enabled:
            return None
        key = self._key(text, model)
        with self._lock:
            val = self._cache.get(key)
            if val is not None:
                self._hits += 1
                return val
            self._misses += 1
            return None

    def set(self, text: str, model: str, vector: list[float]) -> None:
        if not self._enabled:
            return
        key = self._key(text, model)
        with self._lock:
            self._cache[key] = vector

    def stats(self) -> dict[str, int]:
        if not self._enabled:
            return {"hits": 0, "misses": 0, "size": 0}
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
            }
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd shared && python3 -m pytest tests/test_embedding_cache.py -v
```

Expected: all 10 tests pass.

- [ ] **Step 6: Commit**

```bash
git add shared/embedding_cache.py shared/tests/test_embedding_cache.py mcp-server/requirements.txt ingestion/requirements.txt
git commit -m "feat: add in-process TTL embedding cache with Valkey-ready interface"
```

---

### Task 2: Embedding Batch API (`shared/llm_provider.py`)

**Files:**
- Modify: `shared/llm_provider.py:33-45` — add `embed_batch()` method
- Modify: `shared/tests/test_llm_provider.py` — add batch tests

- [ ] **Step 1: Write failing tests for embed_batch**

Append to `shared/tests/test_llm_provider.py`:

```python
# ===========================================================================
# EmbeddingProvider.embed_batch
# ===========================================================================

class TestEmbeddingProviderEmbedBatch:
    async def test_batch_success(self):
        embeddings = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        json_data = {
            "data": [
                {"index": 0, "embedding": embeddings[0]},
                {"index": 1, "embedding": embeddings[1]},
                {"index": 2, "embedding": embeddings[2]},
            ]
        }
        http = _mock_http(json_data=json_data)
        ep = EmbeddingProvider("http://localhost:11434")

        result = await ep.embed_batch(http, ["a", "b", "c"], "nomic-embed-text")

        assert result == embeddings
        http.post.assert_called_once_with(
            "http://localhost:11434/v1/embeddings",
            headers={},
            json={"model": "nomic-embed-text", "input": ["a", "b", "c"]},
        )

    async def test_batch_reorders_by_index(self):
        """OpenAI API may return results out of order."""
        json_data = {
            "data": [
                {"index": 2, "embedding": [0.5]},
                {"index": 0, "embedding": [0.1]},
                {"index": 1, "embedding": [0.3]},
            ]
        }
        http = _mock_http(json_data=json_data)
        ep = EmbeddingProvider("http://host")

        result = await ep.embed_batch(http, ["a", "b", "c"], "model")

        assert result == [[0.1], [0.3], [0.5]]

    async def test_batch_single_item(self):
        json_data = {"data": [{"index": 0, "embedding": [1.0, 2.0]}]}
        http = _mock_http(json_data=json_data)
        ep = EmbeddingProvider("http://host")

        result = await ep.embed_batch(http, ["text"], "model")

        assert result == [[1.0, 2.0]]

    async def test_batch_empty_list(self):
        ep = EmbeddingProvider("http://host")
        http = _mock_http()

        result = await ep.embed_batch(http, [], "model")

        assert result == []
        http.post.assert_not_called()

    async def test_batch_http_error_propagates(self):
        http = _mock_http(status_code=500)
        ep = EmbeddingProvider("http://host")

        with pytest.raises(httpx.HTTPStatusError):
            await ep.embed_batch(http, ["text"], "model")
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd shared && python3 -m pytest tests/test_llm_provider.py::TestEmbeddingProviderEmbedBatch -v
```

Expected: `AttributeError: 'EmbeddingProvider' object has no attribute 'embed_batch'`

- [ ] **Step 3: Implement embed_batch**

In `shared/llm_provider.py`, add method to `EmbeddingProvider` class after `embed()`:

```python
    async def embed_batch(
        self, http: httpx.AsyncClient, texts: list[str], model: str
    ) -> list[list[float]]:
        """Embed multiple texts in a single API call.

        Uses the OpenAI-compatible batch input format (input: list[str]).
        Results are sorted by index to guarantee input order.
        """
        if not texts:
            return []
        resp = await http.post(
            f"{self.base_url}/v1/embeddings",
            headers=self.headers,
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda x: x["index"])
        return [d["embedding"] for d in data]
```

- [ ] **Step 4: Run all llm_provider tests — verify they pass**

```bash
cd shared && python3 -m pytest tests/test_llm_provider.py -v
```

Expected: all tests pass (existing + 5 new).

- [ ] **Step 5: Commit**

```bash
git add shared/llm_provider.py shared/tests/test_llm_provider.py
git commit -m "feat: add embed_batch() for batch embedding via OpenAI-compatible API"
```

---

### Task 3: OPA Result Cache (`mcp-server/server.py`)

**Files:**
- Modify: `mcp-server/server.py:465-493` — add cache around `check_opa_policy()`
- Create: `mcp-server/tests/test_opa_cache.py`

- [ ] **Step 1: Write failing tests**

Create `mcp-server/tests/test_opa_cache.py`:

```python
"""Tests for OPA access policy result caching."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# We test the caching behavior by mocking the HTTP call inside check_opa_policy.
# The cache is module-level in server.py, so we import and test through it.


class TestOpaCache:
    """Test OPA result caching in check_opa_policy()."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """Clear the OPA cache before each test."""
        import server
        if hasattr(server, '_opa_cache'):
            server._opa_cache.clear()
        yield

    @pytest.fixture
    def mock_http(self):
        """Mock the module-level HTTP client."""
        mock = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"result": True}
        mock.post.return_value = resp
        return mock

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self, mock_http):
        """Same (role, classification, action) should hit cache on second call."""
        import server
        server.OPA_CACHE_ENABLED = True
        with patch.object(server, 'http', mock_http), \
             patch.object(server, '_otel_span', return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())):
            # First call — hits OPA
            r1 = await server.check_opa_policy("agent1", "analyst", "res/1", "internal")
            # Second call — same role+classification+action, should use cache
            r2 = await server.check_opa_policy("agent2", "analyst", "res/2", "internal")

            assert r1["allowed"] is True
            assert r2["allowed"] is True
            # OPA HTTP should only be called once
            assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_different_classification_misses_cache(self, mock_http):
        """Different classification should be a cache miss."""
        import server
        server.OPA_CACHE_ENABLED = True
        with patch.object(server, 'http', mock_http), \
             patch.object(server, '_otel_span', return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())):
            await server.check_opa_policy("a1", "analyst", "r/1", "internal")
            await server.check_opa_policy("a1", "analyst", "r/2", "confidential")

            assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_disabled(self, mock_http):
        """When cache is disabled, every call hits OPA."""
        import server
        server.OPA_CACHE_ENABLED = False
        with patch.object(server, 'http', mock_http), \
             patch.object(server, '_otel_span', return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())):
            await server.check_opa_policy("a1", "analyst", "r/1", "internal")
            await server.check_opa_policy("a1", "analyst", "r/2", "internal")

            assert mock_http.post.call_count == 2
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd mcp-server && python3 -m pytest tests/test_opa_cache.py -v
```

Expected: `AttributeError: module 'server' has no attribute '_opa_cache'`

- [ ] **Step 3: Implement OPA cache**

In `mcp-server/server.py`, add after the config block (around line 100):

```python
# ── OPA Cache ────────────────────────────────────────────────
from cachetools import TTLCache as _TTLCache

OPA_CACHE_TTL     = int(os.getenv("OPA_CACHE_TTL", "60"))
OPA_CACHE_ENABLED = os.getenv("OPA_CACHE_ENABLED", "true").lower() == "true"
_opa_cache: _TTLCache[str, bool] = _TTLCache(maxsize=64, ttl=OPA_CACHE_TTL)
_opa_cache_lock = __import__("threading").Lock()
```

Then modify `check_opa_policy()` (around line 472) to check the cache before making the HTTP call. Replace the function:

```python
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
    with _otel_span("opa_check"):
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
```

- [ ] **Step 4: Run OPA cache tests**

```bash
cd mcp-server && python3 -m pytest tests/test_opa_cache.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 5: Run all MCP server tests to check for regressions**

```bash
cd mcp-server && python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add mcp-server/server.py mcp-server/tests/test_opa_cache.py
git commit -m "feat: add TTL cache for OPA access policy results

Caches check_opa_policy() by (role, classification, action).
Reduces 50 OPA calls per search to 1-4. TTL=60s, configurable via OPA_CACHE_TTL."
```

---

### Task 4: Wire Embedding Cache into Services

**Files:**
- Modify: `mcp-server/server.py:346-356` — wrap `embed_text()` with cache
- Modify: `ingestion/ingestion_api.py:157-159` — wrap `get_embedding()` with cache
- Modify: `ingestion/ingestion_api.py:504-505` — use batch embedding in `ingest_text_chunks()`

- [ ] **Step 1: Wire cache into mcp-server**

In `mcp-server/server.py`, add import and instantiation after the embedding provider (around line 76):

```python
from shared.embedding_cache import EmbeddingCache

embedding_cache = EmbeddingCache()
```

Modify `embed_text()` (line 354-356) to use the cache:

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
    before_sleep=lambda rs: log.warning(f"Embed retry #{rs.attempt_number} nach Fehler: {rs.outcome.exception()}"),
)
async def embed_text(text: str) -> list[float]:
    with _otel_span("embed_text"):
        cached = embedding_cache.get(text, EMBEDDING_MODEL)
        if cached is not None:
            return cached
        vector = await embedding_provider.embed(http, text, EMBEDDING_MODEL)
        embedding_cache.set(text, EMBEDDING_MODEL, vector)
        return vector
```

- [ ] **Step 2: Wire cache into ingestion**

In `ingestion/ingestion_api.py`, add import after the existing embedding provider setup (around line 57):

```python
from shared.embedding_cache import EmbeddingCache

embedding_cache = EmbeddingCache()
```

Modify `get_embedding()` (line 157-159):

```python
async def get_embedding(text: str) -> list[float]:
    """Erzeugt Embedding über den konfigurierten Provider (OpenAI-compat), mit Cache."""
    cached = embedding_cache.get(text, EMBEDDING_MODEL)
    if cached is not None:
        return cached
    vector = await embedding_provider.embed(http_client, text, EMBEDDING_MODEL)
    embedding_cache.set(text, EMBEDDING_MODEL, vector)
    return vector
```

- [ ] **Step 3: Add batch embedding to ingestion pipeline**

In `ingestion/ingestion_api.py`, replace the sequential embedding in `ingest_text_chunks()`. The loop at lines 423-524 embeds each chunk individually at line 505 (`embedding = await get_embedding(chunk)`).

Refactor: collect all processed chunks first, then batch-embed. Replace the single-embed call in the chunk loop with a placeholder, then do a batch embed after the loop.

After the chunk processing loop (after line 524, before line 526 `# 5. In Qdrant upserten`), replace the embedding approach:

The current code at line 505 does `embedding = await get_embedding(chunk)` inside the loop. Change the loop to collect texts instead, then batch-embed:

1. Remove `embedding = await get_embedding(chunk)` from inside the loop (line 505)
2. Store processed chunk texts in a list
3. After the loop, batch-embed all texts (cache-aware)
4. Build points from the batch results

This requires restructuring the loop. The new pattern:

```python
    # Collect processed chunks (after PII handling)
    processed_texts: list[str] = []
    chunk_metadata: list[dict] = []  # store per-chunk metadata for point creation

    for i, chunk in enumerate(chunks):
        # ... existing PII scan + handling code (lines 423-503) stays the same ...
        # Instead of embedding here, collect the processed chunk text
        processed_texts.append(chunk)
        chunk_metadata.append({
            "vault_ref": vault_ref,
            "contains_pii": scan_result.contains_pii,
            "chunk_index": i,
        })

    # 4. Batch embedding (cache-aware)
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
        for pos, idx in enumerate(uncached_indices):
            embeddings[idx] = batch_results[pos]
            embedding_cache.set(uncached_texts[pos], EMBEDDING_MODEL, batch_results[pos])

    # 5. Build points
    for idx, (text, emb, meta) in enumerate(zip(processed_texts, embeddings, chunk_metadata)):
        point_id = str(uuid.uuid4())
        payload = {
            "text": text,
            "source": source,
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
        points.append(PointStruct(id=point_id, vector=emb, payload=payload))
        vault_refs.append(meta["vault_ref"])
```

**Critical implementation notes:**

1. **Early return on `block`:** The PII scan loop's early-return on `block` (line 448) must be preserved. It returns immediately before any embedding happens, so it doesn't interact with the batch logic.
2. **Chunk mutation:** The `chunk` variable gets reassigned inside the PII handling (pseudonymize at line 483, mask at line 499). `processed_texts.append(chunk)` MUST come after all PII handling for that chunk to capture the final (pseudonymized/masked) version.
3. **vault_ref tracking:** Each chunk's `vault_ref` is set during PII handling. The `chunk_metadata` list preserves this per-chunk data for point construction after batch embedding.
4. **Existing `points` list:** Remove the `points.append(PointStruct(...))` from inside the current loop (line 521-523) and move it to the new batch-embed section. The `vault_refs.append()` at line 524 also moves.
5. **Error handling:** If `embed_batch` fails, the entire `ingest_text_chunks()` call fails — this matches the current behavior where a single embedding failure fails the whole ingestion.

- [ ] **Step 4: Run ingestion tests to verify**

```bash
cd ingestion && python3 -m pytest tests/ -v
```

Expected: all tests pass. The existing `test_layer_generation.py` mocks `get_embedding` — batch embedding is transparent.

- [ ] **Step 5: Run MCP server tests**

```bash
cd mcp-server && python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add mcp-server/server.py ingestion/ingestion_api.py
git commit -m "feat: wire embedding cache into mcp-server and ingestion

Cache-first lookup in embed_text() and get_embedding(). Ingestion
uses batch embedding with per-item cache check — only misses go
to the provider. Transparent to all callers."
```

---

### Task 5: Pool Size Environment Variables

**Files:**
- Modify: `shared/config.py` — add pool size readers
- Modify: `mcp-server/server.py:1719`
- Modify: `ingestion/ingestion_api.py:89`
- Modify: `pb-proxy/auth.py:34-41`
- Modify: `ingestion/retention_cleanup.py:314`

- [ ] **Step 1: Add pool config to shared/config.py**

Append to `shared/config.py`:

```python

# ── Connection Pool ──────────────────────────────────────────

PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "2"))
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "10"))
```

- [ ] **Step 2: Update mcp-server pool creation**

In `mcp-server/server.py:1719`, change:

```python
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
```

to:

```python
        from shared.config import PG_POOL_MIN, PG_POOL_MAX
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX)
```

- [ ] **Step 3: Update ingestion pool creation**

In `ingestion/ingestion_api.py:89`, change:

```python
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
```

to:

```python
        from shared.config import PG_POOL_MIN, PG_POOL_MAX
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX)
```

- [ ] **Step 4: Update pb-proxy pool creation**

In `pb-proxy/auth.py:34-41`, change:

```python
        self._pool = await asyncpg.create_pool(
            host=config.PG_HOST,
            port=config.PG_PORT,
            database=config.PG_DATABASE,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            min_size=1,
            max_size=5,
        )
```

to:

```python
        self._pool = await asyncpg.create_pool(
            host=config.PG_HOST,
            port=config.PG_PORT,
            database=config.PG_DATABASE,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            min_size=int(os.getenv("PG_POOL_MIN", "1")),
            max_size=int(os.getenv("PG_POOL_MAX", "5")),
        )
```

Note: pb-proxy uses its own config module, not `shared.config`. Read env vars directly to avoid coupling. Default to previous values (1/5) for this service.

- [ ] **Step 5: Update retention_cleanup pool creation**

In `ingestion/retention_cleanup.py:314`, change:

```python
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=5)
```

to:

```python
    from shared.config import PG_POOL_MIN, PG_POOL_MAX
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX)
```

- [ ] **Step 6: Run tests**

```bash
cd mcp-server && python3 -m pytest tests/ -v
cd ../ingestion && python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add shared/config.py mcp-server/server.py ingestion/ingestion_api.py pb-proxy/auth.py ingestion/retention_cleanup.py
git commit -m "feat: make PG pool sizes configurable via PG_POOL_MIN/PG_POOL_MAX env vars"
```

---

### Task 6: Docker Compose Health Checks + MCP Server `/health` Route

**Files:**
- Modify: `mcp-server/server.py:1731-1732` — add `/health` route
- Modify: `docker-compose.yml:170-220` — add health checks for mcp-server
- Modify: `docker-compose.yml:222-258` — add health check for ingestion
- Modify: `docker-compose.yml:128-147` — add health check for ollama

- [ ] **Step 1: Add `/health` route to mcp-server**

In `mcp-server/server.py`, add a health handler before the Starlette app creation (around line 1731). Add import and handler:

```python
from starlette.responses import PlainTextResponse

async def health_check(request):
    return PlainTextResponse("ok")
```

Then modify the routes list (line 1731-1732):

```python
    app = Starlette(
        routes=[
            Route("/health", endpoint=health_check),
            Route(MCP_PATH, endpoint=MCPTransport()),
        ],
        lifespan=lifespan,
    )
```

- [ ] **Step 2: Add health check for mcp-server in docker-compose.yml**

After line 220 (after `restart: unless-stopped` for mcp-server), add:

```yaml
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 10s
```

- [ ] **Step 3: Add health check for ingestion in docker-compose.yml**

After line 258 (after `restart: unless-stopped` for ingestion), add:

```yaml
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8081/health')"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 10s
```

- [ ] **Step 4: Add health check for ollama in docker-compose.yml**

After line 138 (after `restart: unless-stopped` for ollama), add:

```yaml
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:11434/api/tags"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
```

- [ ] **Step 5: Run MCP server tests**

```bash
cd mcp-server && python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add mcp-server/server.py docker-compose.yml
git commit -m "feat: add health checks for mcp-server, ingestion, and ollama

MCP server gets a /health Starlette route. Docker Compose health
checks added for the 3 services that were missing them."
```

---

### Task 7: Configuration Documentation + New Env Vars in Compose

**Files:**
- Modify: `.env.example` — add new env vars
- Modify: `docker-compose.yml` — pass new env vars to services
- Modify: `CLAUDE.md` — update architecture docs

- [ ] **Step 1: Update .env.example**

Add a new section after the `LAYER_GENERATION_ENABLED` block (after line 47):

```
# ── Performance Tuning (T1) ────────────────────────────────
# Embedding cache (in-process LRU, per service)
# EMBEDDING_CACHE_SIZE=2048
# EMBEDDING_CACHE_TTL=3600
# EMBEDDING_CACHE_ENABLED=true

# OPA access policy result cache (MCP server only)
# OPA_CACHE_TTL=60
# OPA_CACHE_ENABLED=true

# PostgreSQL connection pool sizes (all services)
# PG_POOL_MIN=2
# PG_POOL_MAX=10
```

- [ ] **Step 2: Pass new env vars in docker-compose.yml**

Add to the mcp-server environment block (after `SUMMARIZATION_ENABLED` line 210):

```yaml
      EMBEDDING_CACHE_SIZE: ${EMBEDDING_CACHE_SIZE:-2048}
      EMBEDDING_CACHE_TTL: ${EMBEDDING_CACHE_TTL:-3600}
      EMBEDDING_CACHE_ENABLED: ${EMBEDDING_CACHE_ENABLED:-true}
      OPA_CACHE_TTL: ${OPA_CACHE_TTL:-60}
      OPA_CACHE_ENABLED: ${OPA_CACHE_ENABLED:-true}
      PG_POOL_MIN: ${PG_POOL_MIN:-2}
      PG_POOL_MAX: ${PG_POOL_MAX:-10}
```

Add to the ingestion environment block (after `LAYER_GENERATION_ENABLED` line 250):

```yaml
      EMBEDDING_CACHE_SIZE: ${EMBEDDING_CACHE_SIZE:-2048}
      EMBEDDING_CACHE_TTL: ${EMBEDDING_CACHE_TTL:-3600}
      EMBEDDING_CACHE_ENABLED: ${EMBEDDING_CACHE_ENABLED:-true}
      PG_POOL_MIN: ${PG_POOL_MIN:-2}
      PG_POOL_MAX: ${PG_POOL_MAX:-10}
```

- [ ] **Step 3: Update CLAUDE.md**

Add to the "Completed Features" list (after item 17):

```
18. ✅ **T1 Production Hardening** — Embedding cache (in-process LRU), batch embedding API, OPA result cache, configurable PG pool sizes, Docker health checks for all services
```

Add a new subsection under "Key Concepts":

```markdown
### Performance Caches (T1)
- **Embedding Cache** — In-process TTL cache (`shared/embedding_cache.py`). SHA-256 key of `model:text`. Configurable via `EMBEDDING_CACHE_SIZE` (default 2048), `EMBEDDING_CACHE_TTL` (default 3600s), `EMBEDDING_CACHE_ENABLED`.
- **OPA Result Cache** — TTL cache for `check_opa_policy()` in MCP server. Key: `(role, classification, action)`. Only `pb.access.allow` is cached (deterministic). Configurable via `OPA_CACHE_TTL` (default 60s), `OPA_CACHE_ENABLED`.
- **Batch Embedding** — `EmbeddingProvider.embed_batch()` sends multiple texts in one `/v1/embeddings` request. Used by ingestion pipeline with cache-aware partial batching.
```

- [ ] **Step 4: Commit**

```bash
git add .env.example docker-compose.yml CLAUDE.md
git commit -m "docs: add T1 performance tuning env vars and update architecture docs"
```

---

### Task 8: Final Verification

- [ ] **Step 1: Run all unit tests across all services**

```bash
cd mcp-server && python3 -m pytest tests/ -v
cd ../ingestion && python3 -m pytest tests/ -v
cd ../shared && python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 2: Verify no import errors**

```bash
cd mcp-server && python3 -c "from shared.embedding_cache import EmbeddingCache; print('OK')"
cd ../ingestion && python3 -c "from shared.embedding_cache import EmbeddingCache; print('OK')"
```

- [ ] **Step 3: Verify docker-compose.yml is valid**

```bash
docker compose config --quiet
```

Expected: exit code 0, no output.
