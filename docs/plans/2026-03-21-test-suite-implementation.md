# Test-Suite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace 21 structural "source string check" tests with real unit and integration tests covering all 4 services (mcp-server, ingestion, reranker, evaluation).

**Architecture:** Tests live next to their service (`mcp-server/tests/`, `ingestion/tests/`, etc.). Pure functions are tested directly; I/O functions use `respx` for httpx mocking and `AsyncMock` for asyncpg. Integration tests remain in `tests/integration/`. pytest with `asyncio_mode = "auto"` handles all async tests.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, pytest-mock, respx, coverage

---

## Task 1: Test Infrastructure Setup

**Files:**
- Create: `pyproject.toml`
- Create: `requirements-dev.txt`
- Create: `mcp-server/tests/__init__.py`
- Create: `mcp-server/tests/conftest.py`
- Create: `ingestion/tests/__init__.py`
- Create: `ingestion/tests/conftest.py`
- Create: `reranker/tests/__init__.py`
- Create: `reranker/tests/conftest.py`
- Create: `evaluation/tests/__init__.py`
- Create: `evaluation/tests/conftest.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/conftest.py`

**Step 1: Create `requirements-dev.txt`**

```
pytest>=8.0
pytest-asyncio>=0.24
pytest-mock>=3.14
respx>=0.22
coverage>=7.0
```

**Step 2: Create `pyproject.toml`**

```toml
[project]
name = "powerbrain"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
testpaths = [
    "mcp-server/tests",
    "ingestion/tests",
    "reranker/tests",
    "evaluation/tests",
    "tests/integration",
]
asyncio_mode = "auto"
markers = [
    "integration: requires running services (deselect with -m 'not integration')",
]
```

**Step 3: Create `mcp-server/tests/conftest.py`**

```python
"""Shared fixtures for mcp-server tests."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add mcp-server to path so we can import server, graph_service
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mock_pg_pool():
    """AsyncMock of asyncpg.Pool with acquire context manager."""
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    conn.fetch.return_value = []
    conn.fetchrow.return_value = None
    conn.execute.return_value = "INSERT 0 1"
    return pool


@pytest.fixture
def mock_http_client():
    """AsyncMock of httpx.AsyncClient for direct patching."""
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {}
    client.post.return_value = response
    return client
```

**Step 4: Create `ingestion/tests/conftest.py`**

```python
"""Shared fixtures for ingestion tests."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mock_pg_pool():
    """AsyncMock of asyncpg.Pool."""
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    conn.fetch.return_value = []
    conn.fetchrow.return_value = None
    conn.execute.return_value = "INSERT 0 1"
    return pool


@pytest.fixture
def mock_scanner():
    """Mock PIIScanner that returns no PII by default."""
    from pii_scanner import PIIScanResult

    scanner = MagicMock()
    scanner.scan_text.return_value = PIIScanResult()
    scanner.mask_text.return_value = "masked text"
    scanner.pseudonymize_text.return_value = ("pseudonymized text", {})
    return scanner
```

**Step 5: Create `reranker/tests/conftest.py`**

```python
"""Shared fixtures for reranker tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mock_model():
    """Mock CrossEncoder model."""
    model = MagicMock()
    model.predict.return_value = [0.9, 0.1, 0.5]
    return model
```

**Step 6: Create `evaluation/tests/conftest.py`**

```python
"""Shared fixtures for evaluation tests."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

**Step 7: Create `tests/integration/conftest.py`**

```python
"""Shared fixtures for integration tests."""

import os
import pytest

def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless RUN_INTEGRATION_TESTS=1."""
    if os.getenv("RUN_INTEGRATION_TESTS") != "1":
        skip = pytest.mark.skip(reason="Set RUN_INTEGRATION_TESTS=1 to run")
        for item in items:
            item.add_marker(skip)
```

**Step 8: Create all `__init__.py` files**

Empty files in: `mcp-server/tests/`, `ingestion/tests/`, `reranker/tests/`, `evaluation/tests/`, `tests/integration/`.

**Step 9: Install dev dependencies**

Run: `pip install -r requirements-dev.txt`

**Step 10: Verify pytest discovers test paths**

Run: `pytest --collect-only 2>&1 | head -20`
Expected: "no tests ran" (no test files yet), but no errors about missing paths.

**Step 11: Commit**

```bash
git add pyproject.toml requirements-dev.txt \
    mcp-server/tests/ ingestion/tests/ reranker/tests/ evaluation/tests/ \
    tests/integration/conftest.py tests/integration/__init__.py
git commit -m "chore: add test infrastructure — pytest, conftest fixtures, requirements-dev"
```

---

## Task 2: Delete Old Structural Tests + Move Integration Tests

**Files:**
- Delete: 21 structural test files in `tests/`
- Move: `tests/test_auth.py` → `tests/integration/test_auth.py`
- Move: `tests/test_vault_integration.py` → `tests/integration/test_vault_integration.py`
- Delete: `tests/test_injection_prevention.py` (will be replaced by `mcp-server/tests/test_validate_identifier.py`)
- Delete: `tests/test_pseudonymize_fix.py` (will be replaced by `ingestion/tests/test_pii_scanner.py`)

**Step 1: Move integration tests**

```bash
mv tests/test_auth.py tests/integration/test_auth.py
mv tests/test_vault_integration.py tests/integration/test_vault_integration.py
```

**Step 2: Delete all remaining structural tests**

```bash
rm tests/test_agtype_parsing.py tests/test_art17_vault_deletion.py \
   tests/test_audit_pii_protection.py tests/test_eval_opa_filter.py \
   tests/test_find_path_fallback.py tests/test_graph_sync_log.py \
   tests/test_ingestion_cleanup.py tests/test_ingestion_dual_storage.py \
   tests/test_injection_prevention.py tests/test_list_datasets_source_type.py \
   tests/test_mcp_requirements.py tests/test_mcp_vault_access.py \
   tests/test_opa_privacy_extensions.py tests/test_parallel_opa_checks.py \
   tests/test_pg_pool_lifespan.py tests/test_pii_scanner_config.py \
   tests/test_pii_vault_schema.py tests/test_pseudonymize_fix.py \
   tests/test_rate_limiter.py tests/test_retention_vault_cleanup.py \
   tests/test_retry_config.py tests/test_search_first_mvp_docs.py \
   tests/test_search_first_mvp_scripts.py tests/test_search_first_mvp_structure.py
```

**Step 3: Verify no old tests remain**

Run: `ls tests/test_*.py`
Expected: No matches (only `tests/integration/` subdir should have tests).

**Step 4: Commit**

```bash
git add -A tests/
git commit -m "refactor: remove 21 structural tests, move integration tests to tests/integration/"
```

---

## Task 3: Pure Function Tests — `validate_identifier` + `_escape_cypher_value`

**Files:**
- Create: `mcp-server/tests/test_validate_identifier.py`

**Step 1: Write the tests**

```python
"""Tests for graph_service identifier validation and Cypher escaping."""

from graph_service import validate_identifier, _require_identifier, _escape_cypher_value


class TestValidateIdentifier:
    def test_valid_simple(self):
        assert validate_identifier("name") is True

    def test_valid_underscore_prefix(self):
        assert validate_identifier("_private") is True

    def test_valid_with_digits(self):
        assert validate_identifier("col_2") is True

    def test_invalid_starts_with_digit(self):
        assert validate_identifier("2name") is False

    def test_invalid_hyphen(self):
        assert validate_identifier("my-col") is False

    def test_invalid_space(self):
        assert validate_identifier("my col") is False

    def test_invalid_semicolon_injection(self):
        assert validate_identifier("name; DROP TABLE") is False

    def test_invalid_cypher_injection(self):
        assert validate_identifier("n}) RETURN n//") is False

    def test_invalid_empty(self):
        assert validate_identifier("") is False

    def test_invalid_none(self):
        assert validate_identifier(None) is False

    def test_invalid_number(self):
        assert validate_identifier(42) is False


class TestRequireIdentifier:
    def test_valid_passes(self):
        _require_identifier("valid_name", "Label")  # should not raise

    def test_invalid_raises_valueerror(self):
        import pytest
        with pytest.raises(ValueError, match="Ungültiger Label"):
            _require_identifier("invalid-name", "Label")

    def test_context_in_error_message(self):
        import pytest
        with pytest.raises(ValueError, match="Property-Key"):
            _require_identifier("a b c", "Property-Key")


class TestEscapeCypherValue:
    def test_string(self):
        assert _escape_cypher_value("hello") == "'hello'"

    def test_string_with_quotes(self):
        result = _escape_cypher_value("it's a \"test\"")
        assert "\\'" in result or "\\\"" in result

    def test_bool_true(self):
        assert _escape_cypher_value(True) == "true"

    def test_bool_false(self):
        assert _escape_cypher_value(False) == "false"

    def test_int(self):
        assert _escape_cypher_value(42) == "42"

    def test_float(self):
        assert _escape_cypher_value(3.14) == "3.14"

    def test_list(self):
        result = _escape_cypher_value(["a", "b"])
        assert result == "['a', 'b']"

    def test_bool_before_int(self):
        """bool is subclass of int in Python — must be checked first."""
        assert _escape_cypher_value(True) == "true"
        assert _escape_cypher_value(True) != "1"
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_validate_identifier.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_validate_identifier.py
git commit -m "test: add unit tests for validate_identifier and _escape_cypher_value"
```

---

## Task 4: Pure Function Tests — `validate_pii_access_token` + `redact_fields`

**Files:**
- Create: `mcp-server/tests/test_token_validation.py`

**Step 1: Write the tests**

```python
"""Tests for HMAC token validation and PII field redaction."""

import json
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# The function imports VAULT_HMAC_SECRET from the module
import server
from server import validate_pii_access_token, redact_fields

TEST_SECRET = "test-secret-key"


def _make_token(payload: dict, secret: str = TEST_SECRET) -> dict:
    """Helper: create a valid signed token."""
    signature = hmac.new(
        secret.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {**payload, "signature": signature}


class TestValidatePiiAccessToken:
    def setup_method(self):
        self._orig_secret = server.VAULT_HMAC_SECRET
        server.VAULT_HMAC_SECRET = TEST_SECRET

    def teardown_method(self):
        server.VAULT_HMAC_SECRET = self._orig_secret

    def test_valid_token(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        result = validate_pii_access_token(token)
        assert result["valid"] is True
        assert result["reason"] == "ok"

    def test_invalid_signature(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        token["signature"] = "deadbeef" * 8
        result = validate_pii_access_token(token)
        assert result["valid"] is False
        assert "signature" in result["reason"].lower()

    def test_expired_token(self):
        expires = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        result = validate_pii_access_token(token)
        assert result["valid"] is False
        assert "expired" in result["reason"].lower()

    def test_missing_expires_at(self):
        token = _make_token({"purpose": "audit"})
        result = validate_pii_access_token(token)
        # Empty string for expires_at should fail parsing
        assert result["valid"] is False

    def test_invalid_expires_at_format(self):
        token = _make_token({"purpose": "audit", "expires_at": "not-a-date"})
        result = validate_pii_access_token(token)
        assert result["valid"] is False
        assert "format" in result["reason"].lower()

    def test_payload_excludes_signature(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        result = validate_pii_access_token(token)
        assert "signature" not in result["payload"]

    def test_wrong_secret_fails(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires}, secret="wrong-key")
        result = validate_pii_access_token(token)
        assert result["valid"] is False


class TestRedactFields:
    def test_redact_email(self):
        text = "Contact max@example.com for info"
        entities = [{"type": "EMAIL_ADDRESS", "start": 8, "end": 23}]
        result = redact_fields(text, entities, {"email"})
        assert "<EMAIL_ADDRESS>" in result
        assert "max@example.com" not in result

    def test_redact_multiple_types(self):
        text = "Max lives in Berlin, call 0151-12345678"
        entities = [
            {"type": "LOCATION", "start": 13, "end": 19},
            {"type": "PHONE_NUMBER", "start": 26, "end": 39},
        ]
        result = redact_fields(text, entities, {"address", "phone"})
        assert "<LOCATION>" in result
        assert "<PHONE_NUMBER>" in result

    def test_no_redaction_for_unmapped_fields(self):
        text = "Some text with data"
        entities = [{"type": "PERSON", "start": 0, "end": 4}]
        result = redact_fields(text, entities, {"unknown_field"})
        assert result == text

    def test_empty_fields_returns_original(self):
        text = "Some text"
        entities = [{"type": "EMAIL_ADDRESS", "start": 0, "end": 4}]
        result = redact_fields(text, entities, set())
        assert result == text

    def test_empty_entities_returns_original(self):
        text = "Some text"
        result = redact_fields(text, [], {"email"})
        assert result == text

    def test_invalid_offsets_skipped(self):
        text = "Short"
        entities = [{"type": "EMAIL_ADDRESS", "start": 0, "end": 100}]
        result = redact_fields(text, entities, {"email"})
        assert result == text  # bounds check: end > len(text), skipped
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_token_validation.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_token_validation.py
git commit -m "test: add unit tests for HMAC token validation and redact_fields"
```

---

## Task 5: Pure Function Tests — `TokenBucket`

**Files:**
- Create: `mcp-server/tests/test_rate_limiter.py`

**Step 1: Write the tests**

```python
"""Tests for TokenBucket rate limiter."""

import asyncio
import pytest
from server import TokenBucket


class TestTokenBucket:
    @pytest.fixture
    def bucket(self):
        """Bucket with capacity 3, refill 1 token/sec."""
        return TokenBucket(capacity=3.0, refill_rate=1.0)

    async def test_initial_tokens_available(self, bucket):
        allowed, retry_after = await bucket.consume()
        assert allowed is True
        assert retry_after == 0.0

    async def test_exhaust_capacity(self, bucket):
        for _ in range(3):
            allowed, _ = await bucket.consume()
            assert allowed is True

        allowed, retry_after = await bucket.consume()
        assert allowed is False
        assert retry_after > 0.0

    async def test_refill_after_wait(self, bucket):
        # Exhaust all tokens
        for _ in range(3):
            await bucket.consume()

        # Wait for refill (1 token/sec, wait 1.1 sec)
        await asyncio.sleep(1.1)

        allowed, _ = await bucket.consume()
        assert allowed is True

    async def test_capacity_cap(self, bucket):
        """Tokens should not exceed capacity even after long wait."""
        await asyncio.sleep(0.5)  # Would add 0.5 tokens, but already at 3.0
        # Consume 4 times — only 3 should succeed
        results = [await bucket.consume() for _ in range(4)]
        allowed_count = sum(1 for allowed, _ in results if allowed)
        assert allowed_count == 3

    async def test_retry_after_value(self):
        """retry_after should reflect time until next token."""
        bucket = TokenBucket(capacity=1.0, refill_rate=1.0)
        await bucket.consume()  # Use the one token
        allowed, retry_after = await bucket.consume()
        assert allowed is False
        assert 0.0 < retry_after <= 1.0
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_rate_limiter.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_rate_limiter.py
git commit -m "test: add unit tests for TokenBucket rate limiter"
```

---

## Task 6: Pure Function Tests — Evaluation Metrics

**Files:**
- Create: `evaluation/tests/test_metrics.py`

**Step 1: Write the tests**

```python
"""Tests for evaluation metrics (pure functions)."""

from run_eval import precision_at_k, recall_at_k, reciprocal_rank, keyword_coverage


class TestPrecisionAtK:
    def test_perfect_precision(self):
        assert precision_at_k(["a", "b", "c"], ["a", "b", "c"]) == 1.0

    def test_zero_precision(self):
        assert precision_at_k(["x", "y", "z"], ["a", "b", "c"]) == 0.0

    def test_partial_precision(self):
        assert precision_at_k(["a", "x", "b"], ["a", "b", "c"]) == pytest.approx(2 / 3)

    def test_empty_returned(self):
        assert precision_at_k([], ["a", "b"]) == 0.0

    def test_empty_expected(self):
        assert precision_at_k(["a", "b"], []) == 0.0


class TestRecallAtK:
    def test_perfect_recall(self):
        assert recall_at_k(["a", "b", "c"], ["a", "b", "c"]) == 1.0

    def test_partial_recall(self):
        assert recall_at_k(["a", "x"], ["a", "b", "c"]) == pytest.approx(1 / 3)

    def test_empty_expected_returns_one(self):
        """No ground truth = trivially satisfied."""
        assert recall_at_k(["a", "b"], []) == 1.0

    def test_empty_returned(self):
        assert recall_at_k([], ["a", "b"]) == 0.0


class TestReciprocalRank:
    def test_first_position(self):
        assert reciprocal_rank(["a", "b", "c"], ["a"]) == 1.0

    def test_second_position(self):
        assert reciprocal_rank(["x", "a", "c"], ["a"]) == 0.5

    def test_third_position(self):
        assert reciprocal_rank(["x", "y", "a"], ["a"]) == pytest.approx(1 / 3)

    def test_not_found(self):
        assert reciprocal_rank(["x", "y", "z"], ["a"]) == 0.0

    def test_multiple_expected(self):
        """Should return rank of FIRST relevant result."""
        assert reciprocal_rank(["x", "b", "a"], ["a", "b"]) == 0.5


class TestKeywordCoverage:
    def test_all_keywords_found(self):
        texts = ["Python is great", "for machine learning"]
        assert keyword_coverage(texts, ["python", "learning"]) == 1.0

    def test_no_keywords_found(self):
        texts = ["unrelated content"]
        assert keyword_coverage(texts, ["python", "rust"]) == 0.0

    def test_partial_coverage(self):
        texts = ["Python code"]
        assert keyword_coverage(texts, ["python", "rust"]) == 0.5

    def test_case_insensitive(self):
        texts = ["PYTHON is GREAT"]
        assert keyword_coverage(texts, ["python"]) == 1.0

    def test_empty_keywords_returns_one(self):
        assert keyword_coverage(["some text"], []) == 1.0

    def test_empty_texts(self):
        assert keyword_coverage([], ["python"]) == 0.0
```

Note: Add `import pytest` at the top of the file for `pytest.approx`.

**Step 2: Run tests**

Run: `pytest evaluation/tests/test_metrics.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add evaluation/tests/test_metrics.py
git commit -m "test: add unit tests for evaluation metrics (precision, recall, MRR, coverage)"
```

---

## Task 7: Pure Function Tests — `chunk_text`

**Files:**
- Create: `ingestion/tests/test_chunk_text.py`

**Step 1: Write the tests**

```python
"""Tests for ingestion text chunking."""

from ingestion_api import chunk_text


class TestChunkText:
    def test_short_text_single_chunk(self):
        text = "Short text."
        chunks = chunk_text(text, max_chars=100)
        assert chunks == [text]

    def test_exact_max_chars(self):
        text = "x" * 100
        chunks = chunk_text(text, max_chars=100)
        assert chunks == [text]

    def test_splits_long_text(self):
        text = "a" * 250
        chunks = chunk_text(text, max_chars=100, overlap=20)
        assert len(chunks) > 1
        # All chunks <= max_chars
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_overlap_between_chunks(self):
        text = "a" * 250
        chunks = chunk_text(text, max_chars=100, overlap=20)
        # Second chunk should start 20 chars before end of first
        assert len(chunks) >= 2

    def test_covers_entire_text(self):
        text = "Hello world, this is a longer text that needs chunking."
        chunks = chunk_text(text, max_chars=20, overlap=5)
        # Reassemble: each chunk's unique part covers the text
        assert chunks[0][:15] == text[:15]  # first chunk starts at 0
        # Last chunk should contain the end of text
        assert text[-5:] in chunks[-1]

    def test_empty_text(self):
        chunks = chunk_text("", max_chars=100)
        assert chunks == [""]

    def test_default_parameters(self):
        text = "x" * 500
        chunks = chunk_text(text)  # max_chars=1000, overlap=200
        assert chunks == [text]  # 500 < 1000, so single chunk
```

**Step 2: Run tests**

Run: `pytest ingestion/tests/test_chunk_text.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add ingestion/tests/test_chunk_text.py
git commit -m "test: add unit tests for chunk_text"
```

---

## Task 8: Mocked Tests — `embed_text`

**Files:**
- Create: `mcp-server/tests/test_embed_text.py`

**Step 1: Write the tests**

```python
"""Tests for embed_text with mocked Ollama HTTP calls."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import httpx

import server
from server import embed_text


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    """Patch the module-level http client for all tests."""
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


class TestEmbedText:
    async def test_returns_embedding_vector(self, _patch_http):
        expected = [0.1, 0.2, 0.3]
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"embeddings": [expected]}
        _patch_http.post.return_value = response

        result = await embed_text("test query")

        assert result == expected
        _patch_http.post.assert_called_once()
        call_args = _patch_http.post.call_args
        assert "/api/embed" in call_args[0][0]
        assert call_args[1]["json"]["model"] == "nomic-embed-text"
        assert call_args[1]["json"]["input"] == "test query"

    async def test_raises_on_http_error(self, _patch_http):
        response = MagicMock()
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        _patch_http.post.return_value = response

        with pytest.raises(httpx.HTTPStatusError):
            await embed_text("test query")

    async def test_retries_on_connect_error(self, _patch_http):
        """embed_text should retry on ConnectError (tenacity)."""
        response_ok = MagicMock()
        response_ok.raise_for_status = MagicMock()
        response_ok.json.return_value = {"embeddings": [[0.1]]}

        _patch_http.post.side_effect = [
            httpx.ConnectError("connection refused"),
            response_ok,
        ]

        # Patch tenacity wait to avoid real sleep
        with patch("server.embed_text.retry.wait", return_value=0):
            result = await embed_text("retry test")

        assert result == [0.1]
        assert _patch_http.post.call_count == 2

    async def test_retries_on_timeout(self, _patch_http):
        """embed_text should retry on TimeoutException."""
        response_ok = MagicMock()
        response_ok.raise_for_status = MagicMock()
        response_ok.json.return_value = {"embeddings": [[0.5]]}

        _patch_http.post.side_effect = [
            httpx.TimeoutException("timeout"),
            response_ok,
        ]

        with patch("server.embed_text.retry.wait", return_value=0):
            result = await embed_text("timeout test")

        assert result == [0.5]
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_embed_text.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_embed_text.py
git commit -m "test: add unit tests for embed_text with mocked Ollama"
```

---

## Task 9: Mocked Tests — `check_opa_policy` + `filter_by_policy`

**Files:**
- Create: `mcp-server/tests/test_opa_policy.py`

**Step 1: Write the tests**

```python
"""Tests for OPA policy checking and filtering."""

from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
import pytest

import server
from server import check_opa_policy, filter_by_policy


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


class TestCheckOpaPolicy:
    async def test_allow(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        result = await check_opa_policy("agent-1", "analyst", "search", "public")
        assert result["allowed"] is True
        assert result["input"]["agent_role"] == "analyst"

    async def test_deny(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": False}
        _patch_http.post.return_value = response

        result = await check_opa_policy("agent-1", "analyst", "search", "restricted")
        assert result["allowed"] is False

    async def test_fail_closed_on_error(self, _patch_http):
        """Non-retryable errors should deny access (fail-closed)."""
        _patch_http.post.side_effect = Exception("OPA unreachable")

        result = await check_opa_policy("agent-1", "analyst", "search", "internal")
        assert result["allowed"] is False

    async def test_default_action_is_read(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        result = await check_opa_policy("agent-1", "admin", "resource", "public")
        assert result["input"]["action"] == "read"


def _make_hit(hit_id, classification="internal"):
    """Create a mock Qdrant ScoredPoint."""
    hit = SimpleNamespace()
    hit.id = hit_id
    hit.payload = {"classification": classification}
    return hit


class TestFilterByPolicy:
    async def test_filters_denied_hits(self, _patch_http):
        # First call: allow, second call: deny
        allow_resp = MagicMock()
        allow_resp.raise_for_status = MagicMock()
        allow_resp.json.return_value = {"result": True}

        deny_resp = MagicMock()
        deny_resp.raise_for_status = MagicMock()
        deny_resp.json.return_value = {"result": False}

        _patch_http.post.side_effect = [allow_resp, deny_resp]

        hits = [_make_hit("doc-1", "public"), _make_hit("doc-2", "restricted")]
        result = await filter_by_policy(hits, "agent-1", "analyst", "search")

        assert len(result) == 1
        assert result[0].id == "doc-1"

    async def test_empty_hits_returns_empty(self, _patch_http):
        result = await filter_by_policy([], "agent-1", "analyst", "search")
        assert result == []
        _patch_http.post.assert_not_called()

    async def test_all_allowed(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        hits = [_make_hit("a"), _make_hit("b"), _make_hit("c")]
        result = await filter_by_policy(hits, "agent-1", "admin", "search")
        assert len(result) == 3

    async def test_default_classification_is_internal(self, _patch_http):
        """Hits without classification should default to 'internal'."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": True}
        _patch_http.post.return_value = response

        hit = SimpleNamespace(id="x", payload={})  # no classification
        await filter_by_policy([hit], "agent-1", "analyst", "search")

        call_args = _patch_http.post.call_args
        input_data = call_args[1]["json"]["input"]
        assert input_data["classification"] == "internal"
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_opa_policy.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_opa_policy.py
git commit -m "test: add unit tests for check_opa_policy and filter_by_policy"
```

---

## Task 10: Mocked Tests — `rerank_results`

**Files:**
- Create: `mcp-server/tests/test_rerank.py`

**Step 1: Write the tests**

```python
"""Tests for rerank_results with mocked Reranker HTTP calls."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import server
from server import rerank_results


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


class TestRerankResults:
    @pytest.fixture
    def sample_docs(self):
        return [
            {"id": "a", "content": "Doc A", "score": 0.9, "metadata": {}},
            {"id": "b", "content": "Doc B", "score": 0.8, "metadata": {}},
            {"id": "c", "content": "Doc C", "score": 0.7, "metadata": {}},
        ]

    async def test_reranker_enabled(self, _patch_http, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "results": [
                {"id": "c", "original_score": 0.7, "rerank_score": 0.95,
                 "rank": 1, "content": "Doc C", "metadata": {}},
                {"id": "a", "original_score": 0.9, "rerank_score": 0.80,
                 "rank": 2, "content": "Doc A", "metadata": {}},
            ]
        }
        _patch_http.post.return_value = response

        result = await rerank_results("query", sample_docs, top_n=2)

        assert len(result) == 2
        assert result[0]["id"] == "c"  # reranked order
        assert "rerank_score" in result[0]

    async def test_reranker_disabled_returns_truncated(self, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", False)

        result = await rerank_results("query", sample_docs, top_n=2)
        assert len(result) == 2
        assert result[0]["id"] == "a"  # original order

    async def test_empty_docs_returns_empty(self, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        result = await rerank_results("query", [], top_n=5)
        assert result == []

    async def test_graceful_fallback_on_error(self, _patch_http, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        _patch_http.post.side_effect = Exception("Reranker down")

        result = await rerank_results("query", sample_docs, top_n=2)
        # Fallback: original order, truncated
        assert len(result) == 2
        assert result[0]["id"] == "a"

    async def test_graceful_fallback_on_http_error(self, _patch_http, sample_docs, monkeypatch):
        monkeypatch.setattr(server, "RERANKER_ENABLED", True)
        response = MagicMock()
        response.raise_for_status.side_effect = Exception("500 error")
        _patch_http.post.return_value = response

        result = await rerank_results("query", sample_docs, top_n=2)
        assert len(result) == 2  # fallback works
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_rerank.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_rerank.py
git commit -m "test: add unit tests for rerank_results with fallback"
```

---

## Task 11: Mocked Tests — `ApiKeyVerifier`

**Files:**
- Create: `mcp-server/tests/test_auth.py`

**Step 1: Write the tests**

```python
"""Tests for ApiKeyVerifier with mocked PostgreSQL."""

import hashlib
from unittest.mock import AsyncMock, patch
import pytest

import server
from server import ApiKeyVerifier


@pytest.fixture
def verifier():
    return ApiKeyVerifier()


@pytest.fixture
def mock_pool(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setattr(server, "pg_pool", pool)
    return pool


class TestApiKeyVerifier:
    async def test_valid_key_returns_access_token(self, verifier, mock_pool):
        key_hash = hashlib.sha256("kb_test_key".encode()).hexdigest()
        mock_pool.fetchrow.return_value = {
            "agent_id": "agent-1",
            "agent_role": "analyst",
        }
        mock_pool.execute.return_value = None

        result = await verifier.verify_token("kb_test_key")

        assert result is not None
        assert result.client_id == "agent-1"
        assert result.scopes == ["analyst"]
        # Verify hash was used for lookup
        call_args = mock_pool.fetchrow.call_args
        assert key_hash == call_args[0][1]

    async def test_invalid_key_returns_none(self, verifier, mock_pool):
        mock_pool.fetchrow.return_value = None

        result = await verifier.verify_token("kb_invalid_key")
        assert result is None

    async def test_empty_token_returns_none(self, verifier, mock_pool):
        result = await verifier.verify_token("")
        assert result is None
        mock_pool.fetchrow.assert_not_called()

    async def test_last_used_update_failure_does_not_break_auth(self, verifier, mock_pool):
        mock_pool.fetchrow.return_value = {
            "agent_id": "agent-1",
            "agent_role": "developer",
        }
        mock_pool.execute.side_effect = Exception("DB write failed")

        result = await verifier.verify_token("kb_test_key")
        # Auth should still succeed even if last_used update fails
        assert result is not None
        assert result.client_id == "agent-1"
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_auth.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_auth.py
git commit -m "test: add unit tests for ApiKeyVerifier"
```

---

## Task 12: Mocked Tests — `graph_service` CRUD

**Files:**
- Create: `mcp-server/tests/test_graph_crud.py`

**Step 1: Write the tests**

```python
"""Tests for graph_service CRUD with mocked asyncpg pool."""

import json
from unittest.mock import AsyncMock
import pytest

from graph_service import (
    create_node, find_node, delete_node,
    _execute_cypher, validate_identifier,
)


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    conn.execute.return_value = None
    conn.fetch.return_value = []
    return pool


class TestCreateNode:
    async def test_creates_node_with_properties(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch.return_value = [{"result": '{"id": 1, "properties": {"name": "Test"}}'}]

        result = await create_node(mock_pool, "Project", {"name": "Test"})

        assert result.get("id") == 1 or result.get("properties", {}).get("name") == "Test"
        # Verify cypher was executed
        assert conn.fetch.called

    async def test_rejects_invalid_label(self, mock_pool):
        with pytest.raises(ValueError, match="Label"):
            await create_node(mock_pool, "invalid-label", {"name": "x"})

    async def test_rejects_invalid_property_key(self, mock_pool):
        with pytest.raises(ValueError, match="Property-Key"):
            await create_node(mock_pool, "Project", {"invalid key": "x"})


class TestFindNode:
    async def test_returns_matching_nodes(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch.return_value = [
            {"result": '{"id": 1, "properties": {"name": "A"}}'},
            {"result": '{"id": 2, "properties": {"name": "B"}}'},
        ]

        result = await find_node(mock_pool, "Project", {"name": "A"})
        assert len(result) == 2

    async def test_empty_properties_matches_all(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch.return_value = []

        result = await find_node(mock_pool, "Project", {})
        assert result == []
        # Should still execute (WHERE true)
        assert conn.fetch.called


class TestDeleteNode:
    async def test_returns_true(self, mock_pool):
        result = await delete_node(mock_pool, "Project", "node-1")
        assert result is True

    async def test_rejects_invalid_label(self, mock_pool):
        with pytest.raises(ValueError):
            await delete_node(mock_pool, "bad label", "node-1")


class TestExecuteCypher:
    async def test_parses_agtype_result(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        # Simulate AGE agtype output with suffix
        conn.fetch.return_value = [
            {"result": '{"id": 1, "label": "Project", "properties": {"name": "X"}}::vertex'}
        ]

        result = await _execute_cypher(mock_pool, "MATCH (n) RETURN n")
        assert len(result) == 1
        assert result[0].get("id") == 1

    async def test_handles_parse_error_gracefully(self, mock_pool):
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        conn.fetch.return_value = [{"result": "not-json"}]

        result = await _execute_cypher(mock_pool, "MATCH (n) RETURN n")
        assert len(result) == 1
        assert "raw" in result[0]
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_graph_crud.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_graph_crud.py
git commit -m "test: add unit tests for graph_service CRUD operations"
```

---

## Task 13: Mocked Tests — `log_access`

**Files:**
- Create: `mcp-server/tests/test_log_access.py`

**Step 1: Write the tests**

```python
"""Tests for log_access audit logging with mocked I/O."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import server
from server import log_access


@pytest.fixture(autouse=True)
def _patch_globals(monkeypatch):
    mock_http = AsyncMock()
    mock_pool = AsyncMock()
    monkeypatch.setattr(server, "http", mock_http)
    monkeypatch.setattr(server, "pg_pool", mock_pool)
    return mock_http, mock_pool


class TestLogAccess:
    async def test_inserts_audit_log(self, _patch_globals):
        _, mock_pool = _patch_globals

        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=None)

        mock_pool.execute.assert_called_once()
        call_args = mock_pool.execute.call_args[0]
        assert "agent_access_log" in call_args[0]
        assert call_args[1] == "agent-1"

    async def test_pii_scan_replaces_query(self, _patch_globals):
        mock_http, mock_pool = _patch_globals

        scan_response = MagicMock()
        scan_response.raise_for_status = MagicMock()
        scan_response.json.return_value = {
            "contains_pii": True,
            "masked_text": "<PERSON> braucht Hilfe",
            "entity_types": ["PERSON"],
        }
        mock_http.post.return_value = scan_response

        context = {"query": "Max Mustermann braucht Hilfe"}
        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=context)

        # Context should be mutated with masked text
        assert context["query"] == "<PERSON> braucht Hilfe"
        assert context["query_contains_pii"] is True

    async def test_pii_scan_failure_does_not_crash(self, _patch_globals):
        mock_http, mock_pool = _patch_globals
        mock_http.post.side_effect = Exception("Ingestion down")

        context = {"query": "some query"}
        # Should not raise
        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=context)

        # DB insert should still happen
        mock_pool.execute.assert_called_once()

    async def test_no_scan_without_query(self, _patch_globals):
        mock_http, mock_pool = _patch_globals

        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context={"other": "data"})

        mock_http.post.assert_not_called()

    async def test_no_scan_with_none_context(self, _patch_globals):
        mock_http, mock_pool = _patch_globals

        await log_access("agent-1", "analyst", "search", "doc-1",
                         "read", "allow", context=None)

        mock_http.post.assert_not_called()
```

**Step 2: Run tests**

Run: `pytest mcp-server/tests/test_log_access.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add mcp-server/tests/test_log_access.py
git commit -m "test: add unit tests for log_access audit logging"
```

---

## Task 14: Mocked Tests — Reranker `/rerank` Endpoint

**Files:**
- Create: `reranker/tests/test_rerank_endpoint.py`
- Create: `reranker/tests/test_health.py`

**Step 1: Write the rerank endpoint tests**

```python
"""Tests for reranker /rerank endpoint with mocked CrossEncoder model."""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

import service
from service import app


@pytest.fixture(autouse=True)
def _set_model(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(service, "model", mock)
    return mock


@pytest.fixture
def client():
    return TestClient(app)


class TestRerankEndpoint:
    def test_basic_reranking(self, client, _set_model):
        _set_model.predict.return_value = [0.9, 0.1, 0.5]

        resp = client.post("/rerank", json={
            "query": "test query",
            "documents": [
                {"id": "a", "content": "Doc A"},
                {"id": "b", "content": "Doc B"},
                {"id": "c", "content": "Doc C"},
            ],
            "top_n": 2,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["output_count"] == 2
        assert data["results"][0]["id"] == "a"  # highest score 0.9
        assert data["results"][0]["rank"] == 1

    def test_empty_documents(self, client):
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [],
            "top_n": 5,
        })
        assert resp.status_code == 200
        assert resp.json()["output_count"] == 0

    def test_model_not_loaded_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(service, "model", None)
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [{"id": "a", "content": "x"}],
        })
        assert resp.status_code == 503

    def test_batch_too_large_returns_400(self, client, _set_model, monkeypatch):
        monkeypatch.setattr(service, "MAX_BATCH_SIZE", 2)
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [
                {"id": str(i), "content": f"doc {i}"} for i in range(5)
            ],
        })
        assert resp.status_code == 400

    def test_scores_in_response(self, client, _set_model):
        _set_model.predict.return_value = [0.8]
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [{"id": "a", "content": "Doc A", "score": 0.5}],
            "top_n": 1,
        })
        data = resp.json()
        assert data["results"][0]["original_score"] == 0.5
        assert data["results"][0]["rerank_score"] == 0.8
```

**Step 2: Write the health endpoint tests**

```python
"""Tests for reranker /health and /models endpoints."""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

import service
from service import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealthEndpoint:
    def test_health_ok_when_model_loaded(self, client, monkeypatch):
        monkeypatch.setattr(service, "model", MagicMock())
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_loading_when_no_model(self, client, monkeypatch):
        monkeypatch.setattr(service, "model", None)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "loading"


class TestModelsEndpoint:
    def test_lists_model_info(self, client):
        resp = client.get("/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "current" in data
        assert "alternatives" in data
        assert "max_batch_size" in data
```

**Step 3: Run tests**

Run: `pytest reranker/tests/ -v`
Expected: All tests PASS.

**Step 4: Commit**

```bash
git add reranker/tests/test_rerank_endpoint.py reranker/tests/test_health.py
git commit -m "test: add unit tests for reranker endpoints"
```

---

## Task 15: Mocked Tests — PII Scanner

**Files:**
- Create: `ingestion/tests/test_pii_scanner.py`

**Step 1: Write the tests**

```python
"""Tests for PIIScanner with mocked Presidio."""

from unittest.mock import MagicMock, patch
import pytest

from pii_scanner import PIIScanner, PIIScanResult


@pytest.fixture
def scanner():
    """Create a PIIScanner with mocked NLP engine."""
    with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
         patch("pii_scanner.AnalyzerEngine") as mock_analyzer_cls, \
         patch("pii_scanner.AnonymizerEngine") as mock_anonymizer_cls:

        mock_engine = MagicMock()
        mock_provider.return_value.create_engine.return_value = mock_engine

        scanner = PIIScanner(languages=["de"])
        # Store mocks for test access
        scanner._mock_analyzer = scanner.analyzer
        scanner._mock_anonymizer = scanner.anonymizer
        yield scanner


class TestScanText:
    def test_no_pii_returns_empty(self, scanner):
        scanner.analyzer.analyze.return_value = []

        result = scanner.scan_text("Hallo Welt")
        assert result.contains_pii is False
        assert result.entity_counts == {}

    def test_detects_pii(self, scanner):
        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        mock_result.start = 0
        mock_result.end = 15
        mock_result.score = 0.95
        scanner.analyzer.analyze.return_value = [mock_result]

        result = scanner.scan_text("Max Mustermann ist hier")
        assert result.contains_pii is True
        assert result.entity_counts["PERSON"] == 1
        assert len(result.entity_locations) == 1

    def test_empty_text_returns_empty(self, scanner):
        result = scanner.scan_text("")
        assert result.contains_pii is False
        scanner.analyzer.analyze.assert_not_called()

    def test_whitespace_only_returns_empty(self, scanner):
        result = scanner.scan_text("   \n\t  ")
        assert result.contains_pii is False


class TestMaskText:
    def test_masks_pii(self, scanner):
        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        scanner.analyzer.analyze.return_value = [mock_result]

        mock_anonymized = MagicMock()
        mock_anonymized.text = "<PERSON> ist hier"
        scanner.anonymizer.anonymize.return_value = mock_anonymized

        result = scanner.mask_text("Max Mustermann ist hier")
        assert result == "<PERSON> ist hier"


class TestPseudonymizeText:
    def test_deterministic_pseudonyms(self, scanner):
        mock_r1 = MagicMock()
        mock_r1.entity_type = "PERSON"
        mock_r1.start = 0
        mock_r1.end = 3
        mock_r1.score = 0.9
        scanner.analyzer.analyze.return_value = [mock_r1]

        text = "Max is here"
        result1, map1 = scanner.pseudonymize_text(text, "salt1")
        result2, map2 = scanner.pseudonymize_text(text, "salt1")

        # Same salt + same text = same pseudonym
        assert map1 == map2
        assert result1 == result2

    def test_different_salt_different_pseudonym(self, scanner):
        mock_r = MagicMock()
        mock_r.entity_type = "PERSON"
        mock_r.start = 0
        mock_r.end = 3
        mock_r.score = 0.9
        scanner.analyzer.analyze.return_value = [mock_r]

        _, map1 = scanner.pseudonymize_text("Max is here", "salt1")
        _, map2 = scanner.pseudonymize_text("Max is here", "salt2")

        assert map1["Max"] != map2["Max"]

    def test_no_pii_returns_original(self, scanner):
        scanner.analyzer.analyze.return_value = []
        result, mapping = scanner.pseudonymize_text("No PII here", "salt")
        assert result == "No PII here"
        assert mapping == {}
```

**Step 2: Run tests**

Run: `pytest ingestion/tests/test_pii_scanner.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add ingestion/tests/test_pii_scanner.py
git commit -m "test: add unit tests for PIIScanner with mocked Presidio"
```

---

## Task 16: Mocked Tests — `check_opa_privacy`

**Files:**
- Create: `ingestion/tests/test_opa_privacy.py`

**Step 1: Write the tests**

```python
"""Tests for OPA privacy policy checking in ingestion."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import ingestion_api
from ingestion_api import check_opa_privacy


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(ingestion_api, "http_client", mock_client)
    return mock_client


class TestCheckOpaPrivacy:
    async def test_returns_policy_result(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "result": {
                "pii_action": "pseudonymize",
                "dual_storage_enabled": True,
                "retention_days": 180,
            }
        }
        _patch_http.post.return_value = response

        result = await check_opa_privacy("internal", True, "consent")

        assert result["pii_action"] == "pseudonymize"
        assert result["dual_storage_enabled"] is True
        assert result["retention_days"] == 180

    async def test_defaults_to_block_on_error(self, _patch_http):
        _patch_http.post.side_effect = Exception("OPA unreachable")

        result = await check_opa_privacy("internal", True)

        assert result["pii_action"] == "block"
        assert result["dual_storage_enabled"] is False

    async def test_calls_correct_endpoint(self, _patch_http):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": {"pii_action": "redact"}}
        _patch_http.post.return_value = response

        await check_opa_privacy("confidential", False, "legal_obligation")

        call_args = _patch_http.post.call_args
        assert "/v1/data/kb/privacy" in call_args[0][0]
        input_data = call_args[1]["json"]["input"]
        assert input_data["classification"] == "confidential"
        assert input_data["contains_pii"] is False
```

**Step 2: Run tests**

Run: `pytest ingestion/tests/test_opa_privacy.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add ingestion/tests/test_opa_privacy.py
git commit -m "test: add unit tests for check_opa_privacy"
```

---

## Task 17: Mocked Tests — Evaluation `search` + `check_opa_access`

**Files:**
- Create: `evaluation/tests/test_search_with_opa.py`

**Step 1: Write the tests**

```python
"""Tests for evaluation search with OPA filtering."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import run_eval
from run_eval import check_opa_access, _opa_access_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear OPA access cache before each test."""
    run_eval._opa_access_cache.clear()
    yield
    run_eval._opa_access_cache.clear()


class TestCheckOpaAccess:
    async def test_allows_public(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        result = await check_opa_access(client, "public")
        assert result is True

    async def test_denies_restricted(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": False}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        result = await check_opa_access(client, "restricted")
        assert result is False

    async def test_caches_result(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        await check_opa_access(client, "public")
        await check_opa_access(client, "public")

        # Should only call OPA once due to caching
        assert client.post.call_count == 1

    async def test_fail_closed_on_error(self):
        client = AsyncMock()
        client.post.side_effect = Exception("OPA down")

        result = await check_opa_access(client, "internal")
        assert result is False

    async def test_uses_eval_agent_role(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        await check_opa_access(client, "internal")

        call_args = client.post.call_args
        input_data = call_args[1]["json"]["input"]
        assert input_data["agent_role"] == "analyst"
        assert input_data["agent_id"] == "eval-bot"
```

**Step 2: Run tests**

Run: `pytest evaluation/tests/test_search_with_opa.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add evaluation/tests/test_search_with_opa.py
git commit -m "test: add unit tests for evaluation OPA access checking"
```

---

## Task 18: Run Full Suite + Coverage Report

**Step 1: Run all non-integration tests**

```bash
pytest -m "not integration" -v
```
Expected: All tests PASS.

**Step 2: Run with coverage**

```bash
pytest -m "not integration" --cov=mcp-server --cov=ingestion --cov=reranker --cov=evaluation --cov-report=term-missing
```

**Step 3: Final commit with any fixes**

```bash
git add -A
git commit -m "test: complete test suite — 15 test files replacing 21 structural tests"
```

---

## Summary

| Task | What | Files | Type |
|------|------|-------|------|
| 1 | Infrastructure (pyproject.toml, conftest, deps) | 12 files | Setup |
| 2 | Delete old tests, move integration tests | 24 files | Cleanup |
| 3 | validate_identifier + _escape_cypher_value | 1 test file | Pure |
| 4 | validate_pii_access_token + redact_fields | 1 test file | Pure |
| 5 | TokenBucket | 1 test file | Pure |
| 6 | Evaluation metrics | 1 test file | Pure |
| 7 | chunk_text | 1 test file | Pure |
| 8 | embed_text | 1 test file | Mocked |
| 9 | check_opa_policy + filter_by_policy | 1 test file | Mocked |
| 10 | rerank_results | 1 test file | Mocked |
| 11 | ApiKeyVerifier | 1 test file | Mocked |
| 12 | graph_service CRUD | 1 test file | Mocked |
| 13 | log_access | 1 test file | Mocked |
| 14 | Reranker endpoints | 2 test files | Mocked |
| 15 | PIIScanner | 1 test file | Mocked |
| 16 | check_opa_privacy | 1 test file | Mocked |
| 17 | Evaluation search + OPA | 1 test file | Mocked |
| 18 | Full suite + coverage | - | Verification |
