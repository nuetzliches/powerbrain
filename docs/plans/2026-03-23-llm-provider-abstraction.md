# LLM Provider Abstraction — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Embedding and Summarization backends independently configurable via URL and model name, supporting Ollama, vLLM, HF TEI, infinity, and any OpenAI-compatible endpoint.

**Architecture:** A single shared `llm_provider.py` module provides `EmbeddingProvider` and `CompletionProvider` classes that use the OpenAI-compatible `/v1/embeddings` and `/v1/chat/completions` endpoints. Each consumer (mcp-server, ingestion, evaluation, scripts) imports and configures these providers via environment variables. The Ollama-specific API calls (`/api/embed`, `/api/embeddings`, `/api/generate`) are completely replaced. Health checks use `/v1/models` (OpenAI-compat) with fallback to `/api/tags` (Ollama).

**Tech Stack:** Python 3.12+, httpx (async), pytest + pytest-asyncio, OpenAI-compatible REST API

---

## Environment Variables (new)

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_PROVIDER_URL` | `http://ollama:11434` | Base URL for embedding service |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Embedding model name |
| `EMBEDDING_API_KEY` | _(empty)_ | Optional API key for embedding provider |
| `LLM_PROVIDER_URL` | `http://ollama:11434` | Base URL for summarization/generation |
| `LLM_MODEL` | `qwen2.5:3b` | Summarization model name |
| `LLM_API_KEY` | _(empty)_ | Optional API key for LLM provider |

**Backward compatibility:** `OLLAMA_URL` is kept as fallback. If `EMBEDDING_PROVIDER_URL` is not set, it falls back to `OLLAMA_URL`. Same for `LLM_PROVIDER_URL`. This means existing `.env` files continue to work unchanged.

**Removed after migration:**
- `SUMMARIZATION_MODEL` → replaced by `LLM_MODEL` (with fallback)
- Hardcoded `EMBEDDING_MODEL = "nomic-embed-text"` → replaced by env var

---

## Task 1: Create shared `llm_provider.py` module

**Files:**
- Create: `shared/llm_provider.py`
- Create: `shared/__init__.py`
- Create: `shared/tests/__init__.py`
- Create: `shared/tests/test_llm_provider.py`

### Step 1: Write the failing tests

```python
# shared/tests/test_llm_provider.py
"""Tests for LLM Provider abstraction (OpenAI-compatible API)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

import httpx

from shared.llm_provider import EmbeddingProvider, CompletionProvider


# ── EmbeddingProvider ──────────────────────────────────────


class TestEmbeddingProvider:
    """Tests for EmbeddingProvider using OpenAI-compatible /v1/embeddings."""

    def test_init_defaults(self):
        p = EmbeddingProvider(base_url="http://localhost:11434")
        assert p.base_url == "http://localhost:11434"
        assert p.headers == {}

    def test_init_with_api_key(self):
        p = EmbeddingProvider(base_url="http://vllm:8000", api_key="sk-test")
        assert p.headers == {"Authorization": "Bearer sk-test"}

    def test_init_strips_trailing_slash(self):
        p = EmbeddingProvider(base_url="http://vllm:8000/")
        assert p.base_url == "http://vllm:8000"

    async def test_embed_success(self):
        provider = EmbeddingProvider(base_url="http://ollama:11434")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3]}],
            "model": "nomic-embed-text",
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        }
        http = AsyncMock()
        http.post.return_value = mock_response

        result = await provider.embed(http, "hello world", "nomic-embed-text")

        assert result == [0.1, 0.2, 0.3]
        http.post.assert_called_once_with(
            "http://ollama:11434/v1/embeddings",
            headers={},
            json={"model": "nomic-embed-text", "input": "hello world"},
        )

    async def test_embed_with_api_key(self):
        provider = EmbeddingProvider(
            base_url="http://tei:80", api_key="sk-test"
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.4, 0.5]}],
        }
        http = AsyncMock()
        http.post.return_value = mock_response

        await provider.embed(http, "test", "model-x")

        http.post.assert_called_once_with(
            "http://tei:80/v1/embeddings",
            headers={"Authorization": "Bearer sk-test"},
            json={"model": "model-x", "input": "test"},
        )

    async def test_embed_http_error_propagates(self):
        provider = EmbeddingProvider(base_url="http://ollama:11434")
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        http = AsyncMock()
        http.post.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await provider.embed(http, "fail", "model")


# ── CompletionProvider ─────────────────────────────────────


class TestCompletionProvider:
    """Tests for CompletionProvider using OpenAI-compatible /v1/chat/completions."""

    def test_init_defaults(self):
        p = CompletionProvider(base_url="http://ollama:11434")
        assert p.base_url == "http://ollama:11434"
        assert p.headers == {}

    async def test_generate_success(self):
        provider = CompletionProvider(base_url="http://ollama:11434")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "This is a summary."}}],
        }
        http = AsyncMock()
        http.post.return_value = mock_response

        result = await provider.generate(
            http,
            model="qwen2.5:3b",
            system_prompt="Summarize.",
            user_prompt="Some text to summarize.",
        )

        assert result == "This is a summary."
        call_args = http.post.call_args
        assert call_args[0][0] == "http://ollama:11434/v1/chat/completions"
        payload = call_args[1]["json"]
        assert payload["model"] == "qwen2.5:3b"
        assert payload["stream"] is False
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"

    async def test_generate_empty_response_returns_none(self):
        provider = CompletionProvider(base_url="http://ollama:11434")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": ""}}],
        }
        http = AsyncMock()
        http.post.return_value = mock_response

        result = await provider.generate(
            http, model="m", system_prompt="s", user_prompt="p"
        )
        assert result is None

    async def test_generate_with_api_key(self):
        provider = CompletionProvider(
            base_url="http://vllm:8000", api_key="sk-prod"
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
        }
        http = AsyncMock()
        http.post.return_value = mock_response

        await provider.generate(
            http, model="m", system_prompt="s", user_prompt="p"
        )

        assert http.post.call_args[1]["headers"] == {
            "Authorization": "Bearer sk-prod"
        }

    async def test_generate_http_error_propagates(self):
        provider = CompletionProvider(base_url="http://ollama:11434")
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        http = AsyncMock()
        http.post.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await provider.generate(
                http, model="m", system_prompt="s", user_prompt="p"
            )


# ── Health check ───────────────────────────────────────────


class TestHealthCheck:

    async def test_health_check_openai_compat(self):
        """Provider health via /v1/models (OpenAI-compat)."""
        provider = EmbeddingProvider(base_url="http://tei:80")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        http = AsyncMock()
        http.get.return_value = mock_resp

        result = await provider.health_check(http)
        assert result is True
        http.get.assert_called_once_with(
            "http://tei:80/v1/models", headers={}
        )

    async def test_health_check_failure(self):
        provider = EmbeddingProvider(base_url="http://tei:80")
        http = AsyncMock()
        http.get.side_effect = httpx.ConnectError("refused")

        result = await provider.health_check(http)
        assert result is False
```

### Step 2: Run tests to verify they fail

Run: `python -m pytest shared/tests/test_llm_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared'`

### Step 3: Write minimal implementation

```python
# shared/__init__.py
```

```python
# shared/tests/__init__.py
```

```python
# shared/llm_provider.py
"""
OpenAI-compatible LLM Provider abstraction.

Supports any backend that implements the OpenAI API:
Ollama (>=0.1.24), vLLM, HuggingFace TEI, infinity-embedding, OpenAI, etc.

Usage:
    embedding_provider = EmbeddingProvider(base_url="http://ollama:11434")
    vector = await embedding_provider.embed(http, "hello", "nomic-embed-text")

    llm_provider = CompletionProvider(base_url="http://ollama:11434")
    text = await llm_provider.generate(http, model="qwen2.5:3b",
                                        system_prompt="Summarize.",
                                        user_prompt="...")
"""

from __future__ import annotations

import httpx


class _BaseProvider:
    """Base class for OpenAI-compatible providers."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.headers: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )

    async def health_check(self, http: httpx.AsyncClient) -> bool:
        """Check provider health via GET /v1/models."""
        try:
            resp = await http.get(
                f"{self.base_url}/v1/models", headers=self.headers
            )
            return resp.status_code == 200
        except Exception:
            return False


class EmbeddingProvider(_BaseProvider):
    """
    Embeds text via POST /v1/embeddings (OpenAI-compatible).

    Works with: Ollama, vLLM, HuggingFace TEI, infinity-embedding, OpenAI.
    """

    async def embed(
        self, http: httpx.AsyncClient, text: str, model: str
    ) -> list[float]:
        """Return embedding vector for the given text."""
        resp = await http.post(
            f"{self.base_url}/v1/embeddings",
            headers=self.headers,
            json={"model": model, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


class CompletionProvider(_BaseProvider):
    """
    Generates text via POST /v1/chat/completions (OpenAI-compatible).

    Works with: Ollama, vLLM, OpenAI, Anthropic (via proxy), etc.
    """

    async def generate(
        self,
        http: httpx.AsyncClient,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str | None:
        """Generate a completion. Returns None if empty."""
        resp = await http.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self.headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return content.strip() or None
```

### Step 4: Run tests to verify they pass

Run: `python -m pytest shared/tests/test_llm_provider.py -v`
Expected: All 12 tests PASS

### Step 5: Commit

```bash
git add shared/
git commit -m "feat: add OpenAI-compatible LLM provider abstraction (shared/llm_provider.py)"
```

---

## Task 2: Migrate `mcp-server/server.py` to use providers

**Files:**
- Modify: `mcp-server/server.py` (env vars + `embed_text` + `summarize_text`)
- Modify: `mcp-server/tests/test_embed_text.py`
- Modify: `mcp-server/tests/test_summarize.py`

### Step 1: Update env vars and provider initialization in `server.py`

Replace the old config block (lines ~58, 72-73, 85):

**Old:**
```python
OLLAMA_URL    = os.getenv("OLLAMA_URL",    "http://localhost:11434")
# ...
SUMMARIZATION_MODEL   = os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b")
SUMMARIZATION_ENABLED = os.getenv("SUMMARIZATION_ENABLED", "true").lower() == "true"
# ...
EMBEDDING_MODEL    = "nomic-embed-text"
```

**New:**
```python
# ── Backward-compat fallback ──
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# ── Embedding provider ──
EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL        = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY      = os.getenv("EMBEDDING_API_KEY", "")

# ── LLM / Summarization provider ──
LLM_PROVIDER_URL       = os.getenv("LLM_PROVIDER_URL", _OLLAMA_URL)
LLM_MODEL              = os.getenv("LLM_MODEL", os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b"))
LLM_API_KEY            = os.getenv("LLM_API_KEY", "")
SUMMARIZATION_ENABLED  = os.getenv("SUMMARIZATION_ENABLED", "true").lower() == "true"
```

Add import and provider instantiation (after imports, before app setup):

```python
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.llm_provider import EmbeddingProvider, CompletionProvider

embedding_provider = EmbeddingProvider(base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY)
llm_provider       = CompletionProvider(base_url=LLM_PROVIDER_URL, api_key=LLM_API_KEY)
```

### Step 2: Replace `embed_text()` function

**Old (lines 312-325):**
```python
@retry(...)
async def embed_text(text: str) -> list[float]:
    with _otel_span("embed_text"):
        resp = await http.post(f"{OLLAMA_URL}/api/embed", json={
            "model": EMBEDDING_MODEL, "input": text
        })
        resp.raise_for_status()
        return resp.json()["embeddings"][0]
```

**New:**
```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
    before_sleep=lambda rs: log.warning(
        f"Embed retry #{rs.attempt_number} nach Fehler: {rs.outcome.exception()}"
    ),
)
async def embed_text(text: str) -> list[float]:
    with _otel_span("embed_text"):
        return await embedding_provider.embed(http, text, EMBEDDING_MODEL)
```

### Step 3: Replace `summarize_text()` function

**Old (lines 328-365):** Uses `OLLAMA_URL/api/generate` with Ollama-specific payload.

**New:**
```python
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

    with _otel_span("summarize_text"):
        try:
            return await llm_provider.generate(
                http,
                model=LLM_MODEL,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as e:
            log.warning(f"Summarization failed, returning raw chunks: {e}")
            return None
```

### Step 4: Update existing tests

Update `test_embed_text.py` — the mock now needs to return OpenAI-compat response format:

```python
# Change mock response from:
#   {"embeddings": [[0.1, 0.2, 0.3]]}
# To:
#   {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

# Change expected URL from:
#   f"{server.OLLAMA_URL}/api/embed"
# To:
#   f"{server.EMBEDDING_PROVIDER_URL}/v1/embeddings"
```

Update `test_summarize.py` — mock response format changes:

```python
# Change mock response from:
#   {"response": "This is a summary."}
# To:
#   {"choices": [{"message": {"content": "This is a summary."}}]}

# Change expected URL from:
#   f"{server.OLLAMA_URL}/api/generate"
# To:
#   f"{server.LLM_PROVIDER_URL}/v1/chat/completions"

# Change monkeypatch from:
#   monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "qwen2.5:3b")
# To:
#   monkeypatch.setattr(server, "LLM_MODEL", "qwen2.5:3b")
```

### Step 5: Run tests to verify they pass

Run: `python -m pytest mcp-server/tests/test_embed_text.py mcp-server/tests/test_summarize.py -v`
Expected: All tests PASS

### Step 6: Run full mcp-server test suite

Run: `python -m pytest mcp-server/tests/ -v`
Expected: All tests PASS

### Step 7: Commit

```bash
git add mcp-server/server.py mcp-server/tests/
git commit -m "refactor: migrate mcp-server to OpenAI-compatible LLM provider"
```

---

## Task 3: Migrate `ingestion/ingestion_api.py` to use providers

**Files:**
- Modify: `ingestion/ingestion_api.py` (env vars + `get_embedding` + health check)

### Step 1: Update env vars and provider initialization

**Old (lines 39, 42):**
```python
OLLAMA_URL   = os.getenv("OLLAMA_URL",    "http://ollama:11434")
EMBEDDING_MODEL = "nomic-embed-text"
```

**New:**
```python
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")

EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL        = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY      = os.getenv("EMBEDDING_API_KEY", "")
```

Add import and provider:
```python
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.llm_provider import EmbeddingProvider

embedding_provider = EmbeddingProvider(
    base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY
)
```

### Step 2: Replace `get_embedding()` function

**Old (lines 132-139):**
```python
async def get_embedding(text: str) -> list[float]:
    resp = await http_client.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": text},
    )
    resp.raise_for_status()
    return resp.json()["embedding"]
```

**New:**
```python
async def get_embedding(text: str) -> list[float]:
    """Erzeugt Embedding über den konfigurierten Provider (OpenAI-compat)."""
    return await embedding_provider.embed(http_client, text, EMBEDDING_MODEL)
```

### Step 3: Update health check

**Old (lines 538-543):**
```python
    try:
        await http_client.get(f"{OLLAMA_URL}/api/tags")
        checks["services"]["ollama"] = "ok"
    except Exception:
        checks["services"]["ollama"] = "error"
```

**New:**
```python
    try:
        healthy = await embedding_provider.health_check(http_client)
        checks["services"]["embedding_provider"] = "ok" if healthy else "error"
    except Exception:
        checks["services"]["embedding_provider"] = "error"
```

### Step 4: Run ingestion tests

Run: `python -m pytest ingestion/tests/ -v`
Expected: All tests PASS

### Step 5: Commit

```bash
git add ingestion/ingestion_api.py
git commit -m "refactor: migrate ingestion to OpenAI-compatible embedding provider"
```

---

## Task 4: Migrate `evaluation/run_eval.py` and scripts

**Files:**
- Modify: `evaluation/run_eval.py`
- Modify: `scripts/seed_demo_search_data.py`
- Modify: `testdata/seed.py`

### Step 1: Update `evaluation/run_eval.py`

**Old (lines 35, 39, 109-114):**
```python
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://ollama:11434")
EMBEDDING_MODEL    = "nomic-embed-text"

async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]:
    resp = await client.post(f"{OLLAMA_URL}/api/embed", json={
        "model": EMBEDDING_MODEL, "input": text
    })
    resp.raise_for_status()
    return resp.json()["embeddings"][0]
```

**New:**
```python
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL        = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY      = os.getenv("EMBEDDING_API_KEY", "")

import sys, os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
from shared.llm_provider import EmbeddingProvider

_embedding_provider = EmbeddingProvider(
    base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY
)

async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]:
    return await _embedding_provider.embed(client, text, EMBEDDING_MODEL)
```

### Step 2: Update `scripts/seed_demo_search_data.py`

This script uses synchronous `urllib` — convert its `embed_text()` to use OpenAI-compat endpoint directly (no shared provider needed since it's sync):

**Old (lines 42-48):**
```python
def embed_text(text: str) -> list[float]:
    response = request_json(
        "POST",
        f"{OLLAMA_URL}/api/embed",
        {"model": EMBEDDING_MODEL, "input": text},
    )
    return response["embeddings"][0]
```

**New:**
```python
def embed_text(text: str) -> list[float]:
    url = os.environ.get("EMBEDDING_PROVIDER_URL", OLLAMA_URL)
    response = request_json(
        "POST",
        f"{url}/v1/embeddings",
        {"model": EMBEDDING_MODEL, "input": text},
    )
    return response["data"][0]["embedding"]
```

### Step 3: Update `testdata/seed.py`

The seed script uses Ollama-specific `/api/tags` and `/api/pull` for model management. These are Ollama-only operations (no OpenAI-compat equivalent). Keep them but make them conditional:

**Old `ensure_ollama_model()` (lines 135-151):**

**New:**
```python
def ensure_ollama_model() -> None:
    """Pull the embedding model if not already present (Ollama only)."""
    # Model pull is an Ollama-specific operation.
    # Skip if using a non-Ollama embedding provider.
    embedding_url = os.environ.get("EMBEDDING_PROVIDER_URL", OLLAMA_URL)
    if embedding_url != OLLAMA_URL:
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
```

### Step 4: Commit

```bash
git add evaluation/run_eval.py scripts/seed_demo_search_data.py testdata/seed.py
git commit -m "refactor: migrate evaluation and scripts to OpenAI-compatible provider"
```

---

## Task 5: Update `docker-compose.yml` and `.env.example`

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

### Step 1: Update `.env.example`

Add new section after the existing Summarization block:

```env
# ── LLM / Embedding Provider ──────────────────────────────
# Separate URLs for embedding and LLM (summarization) providers.
# Supports: Ollama (>=0.1.24), vLLM, HuggingFace TEI, infinity, OpenAI.
# Falls back to OLLAMA_URL if not set (backward compatible).
# EMBEDDING_PROVIDER_URL=http://ollama:11434
# EMBEDDING_MODEL=nomic-embed-text
# EMBEDDING_API_KEY=
# LLM_PROVIDER_URL=http://ollama:11434
# LLM_MODEL=qwen2.5:3b
# LLM_API_KEY=

# ── GPU Stack (optional, docker compose --profile gpu) ────
# VLLM_MODEL=llava-hf/llava-1.5-7b-hf
# HF_TOKEN=
```

Update the old Summarization section to reference the new vars:

```env
# ── Summarization ─────────────────────────────────────────
# Legacy vars (still work, new vars above take precedence):
# SUMMARIZATION_MODEL=qwen2.5:3b    → use LLM_MODEL instead
SUMMARIZATION_ENABLED=true
```

### Step 2: Update `docker-compose.yml` — mcp-server environment

Add new env vars to the mcp-server service (keep `OLLAMA_URL` for backward compat):

```yaml
    environment:
      # ... existing vars ...
      OLLAMA_URL: http://ollama:11434
      EMBEDDING_PROVIDER_URL: ${EMBEDDING_PROVIDER_URL:-http://ollama:11434}
      EMBEDDING_MODEL: ${EMBEDDING_MODEL:-nomic-embed-text}
      EMBEDDING_API_KEY: ${EMBEDDING_API_KEY:-}
      LLM_PROVIDER_URL: ${LLM_PROVIDER_URL:-http://ollama:11434}
      LLM_MODEL: ${LLM_MODEL:-${SUMMARIZATION_MODEL:-qwen2.5:3b}}
      LLM_API_KEY: ${LLM_API_KEY:-}
      SUMMARIZATION_ENABLED: ${SUMMARIZATION_ENABLED:-true}
      # Remove: SUMMARIZATION_MODEL (replaced by LLM_MODEL)
```

### Step 3: Update `docker-compose.yml` — ingestion environment

```yaml
    environment:
      # ... existing vars ...
      OLLAMA_URL: http://ollama:11434
      EMBEDDING_PROVIDER_URL: ${EMBEDDING_PROVIDER_URL:-http://ollama:11434}
      EMBEDDING_MODEL: ${EMBEDDING_MODEL:-nomic-embed-text}
      EMBEDDING_API_KEY: ${EMBEDDING_API_KEY:-}
```

### Step 4: Update `docker-compose.yml` — seed environment

```yaml
    environment:
      # ... existing vars ...
      EMBEDDING_PROVIDER_URL: ${EMBEDDING_PROVIDER_URL:-http://ollama:11434}
      EMBEDDING_MODEL: ${EMBEDDING_MODEL:-nomic-embed-text}
```

### Step 5: Add GPU profile services (vLLM + TEI) from ADR T-5

Append to `docker-compose.yml` (before `volumes:` section):

```yaml
  # ── vLLM (optional, replaces Ollama for LLM/VLM) ──
  vllm:
    image: vllm/vllm-openai:latest
    container_name: kb-vllm
    profiles: ["gpu"]
    ports:
      - "8000:8000"
    volumes:
      - vllm_models:/root/.cache/huggingface
    environment:
      HUGGING_FACE_HUB_TOKEN: ${HF_TOKEN:-}
    command:
      - "--model"
      - "${VLLM_MODEL:-llava-hf/llava-1.5-7b-hf}"
      - "--dtype"
      - "bfloat16"
      - "--max-model-len"
      - "4096"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    networks:
      - kb-net
    restart: unless-stopped

  # ── HF Text Embeddings Inference (optional) ──
  tei:
    image: ghcr.io/huggingface/text-embeddings-inference:latest
    container_name: kb-tei
    profiles: ["gpu"]
    ports:
      - "8010:80"
    volumes:
      - tei_models:/data
    command:
      - "--model-id"
      - "${TEI_MODEL:-nomic-ai/nomic-embed-text-v1}"
      - "--port"
      - "80"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    networks:
      - kb-net
    restart: unless-stopped
```

Add volumes:
```yaml
volumes:
  # ... existing volumes ...
  vllm_models:
  tei_models:
```

### Step 6: Commit

```bash
git add docker-compose.yml .env.example
git commit -m "feat: add provider URL config to compose + GPU profile (vLLM, TEI)"
```

---

## Task 6: Update documentation

**Files:**
- Modify: `CLAUDE.md` — update env var table, key concepts, components
- Modify: `README.md` — update quick start if needed

### Step 1: Update CLAUDE.md

Update the "Key Decisions" table:
```markdown
| LLM Provider | OpenAI-compat (shared/llm_provider.py) | Direct Ollama API | Supports vLLM, TEI, infinity, any OpenAI-compat |
```

Update "Components and Ports" to mention configurable providers.

Add to "Key Concepts" section:
```markdown
### LLM Provider Abstraction
Embedding and Summarization use the OpenAI-compatible API (`/v1/embeddings`, `/v1/chat/completions`).
Each can be pointed to a different backend via environment variables:
- `EMBEDDING_PROVIDER_URL` + `EMBEDDING_MODEL` — for vector embeddings
- `LLM_PROVIDER_URL` + `LLM_MODEL` — for summarization/generation
Falls back to `OLLAMA_URL` if not set. Supports Ollama, vLLM, HF TEI, infinity, OpenAI.
Optional GPU stack: `docker compose --profile gpu up -d` (vLLM + HF TEI).
```

### Step 2: Commit

```bash
git add CLAUDE.md README.md
git commit -m "docs: document LLM provider abstraction and GPU stack"
```

---

## Task 7: Run full test suite and verify

### Step 1: Run all tests

```bash
python -m pytest mcp-server/tests/ ingestion/tests/ shared/tests/ -v
```

Expected: All tests PASS

### Step 2: Verify Docker build

```bash
docker compose build mcp-server ingestion
```

Expected: Builds succeed (shared/ must be accessible in Docker context)

### Step 3: Final commit (if any fixes needed)

```bash
git add -A
git commit -m "fix: address test/build issues from provider migration"
```

---

## Summary of Changes

| File | Change |
|---|---|
| `shared/llm_provider.py` | **NEW** — EmbeddingProvider + CompletionProvider |
| `shared/tests/test_llm_provider.py` | **NEW** — 12 unit tests |
| `mcp-server/server.py` | Replace Ollama-specific calls with provider |
| `mcp-server/tests/test_embed_text.py` | Update mock format (OpenAI-compat) |
| `mcp-server/tests/test_summarize.py` | Update mock format (OpenAI-compat) |
| `ingestion/ingestion_api.py` | Replace Ollama-specific calls with provider |
| `evaluation/run_eval.py` | Replace Ollama-specific calls with provider |
| `scripts/seed_demo_search_data.py` | Switch to `/v1/embeddings` |
| `testdata/seed.py` | Conditional model pull (Ollama-only) |
| `docker-compose.yml` | Add new env vars + GPU profile (vLLM, TEI) |
| `.env.example` | Document new env vars |
| `CLAUDE.md` | Update architecture docs |

## Docker Note

The `shared/` module needs to be accessible from the `mcp-server/`, `ingestion/`, and `evaluation/` Docker builds. Two approaches:
1. **Copy in Dockerfile** — Add `COPY shared/ /app/shared/` to each service's Dockerfile (the Docker build context is the project root in `docker-compose.yml`)
2. **Symlink** — Less reliable cross-platform

Recommended: Approach 1. Add to each affected Dockerfile:
```dockerfile
COPY shared/ /app/shared/
```

Verify the `build.context` in `docker-compose.yml` for each service includes the project root (or adjust `context` + `dockerfile`).
