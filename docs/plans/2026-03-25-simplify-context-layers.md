# Simplify Context Layers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the redundant OPA layer access control, the unused backfill script, and the `SUMMARIZATION_MODEL` backward-compat fallback. Keep layers as a pure progressive-loading mechanism.

**Architecture:** Layers (L0/L1/L2) remain as a token-optimization feature for agents. Access control stays with `pb.access`, content restriction stays with `pb.summarization`. The `_check_max_layer` OPA call in the MCP server is removed — if a user has access to a document (via `pb.access`), they can request any layer. The backfill script is deleted (no existing data to migrate). All existing layer-specific tests are deleted and replaced with minimal new tests covering the simplified behavior.

**Tech Stack:** Python 3.12, OPA/Rego, PostgreSQL, Qdrant, pytest

---

## File Inventory

### Delete entirely
- `ingestion/backfill_layers.py` (577 lines — migration script, no users)
- `ingestion/tests/test_layer_generation.py` (451 lines — tests for old code)
- `mcp-server/tests/test_layer_search.py` (191 lines — tests for OPA layer check)
- `opa-policies/pb/layers.rego` (55 lines — redundant access control)
- `opa-policies/pb/test_layers.rego` (56 lines — tests for deleted policy)
- `docs/plans/2026-03-24-context-layers-l0-l1-l2.md` (old plan, superseded)

### Modify
- `mcp-server/server.py` — Remove `_check_max_layer()`, remove OPA layer check from `get_document`, keep `_build_qdrant_filter` layer support and `get_document` tool
- `ingestion/ingestion_api.py` — Remove `SUMMARIZATION_MODEL` fallback from `LLM_MODEL`
- `docker-compose.yml` — Remove `SUMMARIZATION_MODEL` fallback from mcp-server `LLM_MODEL`
- `.env.example` — Remove legacy `SUMMARIZATION_MODEL` comment
- `CLAUDE.md` — Update Context Layers and Summarization documentation
- `docs/architektur.md` — Update layer section (remove OPA access control description)

### Create (tests)
- `mcp-server/tests/test_layer_filter.py` — Tests for `_build_qdrant_filter` with layer param (subset of old tests, no OPA)
- `ingestion/tests/test_layer_generation.py` — Rewrite: tests for L0/L1 generation and ingestion pipeline integration

---

### Task 1: Delete dead files

**Files:**
- Delete: `ingestion/backfill_layers.py`
- Delete: `opa-policies/pb/layers.rego`
- Delete: `opa-policies/pb/test_layers.rego`
- Delete: `docs/plans/2026-03-24-context-layers-l0-l1-l2.md`
- Delete: `ingestion/tests/test_layer_generation.py`
- Delete: `mcp-server/tests/test_layer_search.py`

- [ ] **Step 1: Delete files**

```bash
rm ingestion/backfill_layers.py
rm opa-policies/pb/layers.rego
rm opa-policies/pb/test_layers.rego
rm docs/plans/2026-03-24-context-layers-l0-l1-l2.md
rm ingestion/tests/test_layer_generation.py
rm mcp-server/tests/test_layer_search.py
```

- [ ] **Step 2: Verify OPA tests still pass without layers.rego**

```bash
docker exec pb-opa /opa test /policies/pb/ -v
```

Other policies should not reference `pb.layers`. If OPA is not running, verify no cross-references:

```bash
grep -r "pb.layers\|import data.pb.layers" opa-policies/pb/
```

Expected: no matches.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "chore: delete backfill script, OPA layer policy, and old layer tests"
```

---

### Task 2: Remove `_check_max_layer` from MCP server

**Files:**
- Modify: `mcp-server/server.py:496-511` (delete `_check_max_layer` function)
- Modify: `mcp-server/server.py:1692-1697` (delete OPA layer check in `get_document`)

- [ ] **Step 1: Delete `_check_max_layer` function**

Remove lines 496-511 in `mcp-server/server.py`:

```python
# DELETE this entire function:
async def _check_max_layer(agent_role: str, classification: str) -> str:
    """Query OPA for the maximum allowed layer for a role/classification combo.

    Returns "L2" on OPA failure (permissive fallback — access control is handled
    separately by check_opa_policy).
    """
    input_data = {"agent_role": agent_role, "classification": classification}
    try:
        resp = await http.post(
            f"{OPA_URL}/v1/data/pb/layers/max_layer", json={"input": input_data}
        )
        resp.raise_for_status()
        return resp.json().get("result", "L2")
    except Exception as e:
        log.warning(f"OPA layer check failed, defaulting to L2: {e}")
        return "L2"
```

- [ ] **Step 2: Remove OPA layer restriction from `get_document` handler**

In `mcp-server/server.py`, inside the `get_document` handler (~line 1692-1697), delete:

```python
            # OPA layer restriction
            max_layer = await _check_max_layer(agent_role, classification)
            layer_order = {"L0": 0, "L1": 1, "L2": 2}
            if layer_order.get(layer, 2) > layer_order.get(max_layer, 2):
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"Layer {layer} not allowed", "max_layer": max_layer}))]
```

Keep the OPA access check (`check_opa_policy`) that precedes it — that controls *whether* you see the document, not *which layer*.

- [ ] **Step 3: Run existing MCP server tests**

```bash
cd mcp-server && python3 -m pytest tests/ -v
```

Expected: all pass (the deleted test file is gone, remaining tests should not reference `_check_max_layer`).

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor: remove OPA layer access control from MCP server

Layers are a progressive-loading mechanism, not a security boundary.
Access control is handled by pb.access, content restriction by pb.summarization."
```

---

### Task 3: Clean up `SUMMARIZATION_MODEL` fallback

**Files:**
- Modify: `mcp-server/server.py:72`
- Modify: `ingestion/ingestion_api.py:61`
- Modify: `docker-compose.yml:196`
- Modify: `.env.example:42`

- [ ] **Step 1: Remove SUMMARIZATION_MODEL fallback from mcp-server/server.py**

Change line 72 from:
```python
LLM_MODEL              = os.getenv("LLM_MODEL", os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b"))
```
to:
```python
LLM_MODEL              = os.getenv("LLM_MODEL", "qwen2.5:3b")
```

- [ ] **Step 2: Remove SUMMARIZATION_MODEL fallback from ingestion/ingestion_api.py**

Change line 61 from:
```python
LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b"))
```
to:
```python
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b")
```

- [ ] **Step 3: Remove SUMMARIZATION_MODEL fallback from docker-compose.yml**

Change line 196 (mcp-server service) from:
```yaml
      LLM_MODEL:       ${LLM_MODEL:-${SUMMARIZATION_MODEL:-qwen2.5:3b}}
```
to:
```yaml
      LLM_MODEL:       ${LLM_MODEL:-qwen2.5:3b}
```

- [ ] **Step 4: Clean up .env.example**

Remove legacy comment on line 42:
```
# Legacy: SUMMARIZATION_MODEL is still supported but LLM_MODEL takes precedence
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: remove SUMMARIZATION_MODEL backward-compat fallback"
```

---

### Task 4: Write new layer filter tests (MCP server)

**Files:**
- Create: `mcp-server/tests/test_layer_filter.py`

Tests for `_build_qdrant_filter` with layer parameter — no OPA, pure filter logic.

- [ ] **Step 1: Write the test file**

```python
"""Tests for _build_qdrant_filter with layer parameter support."""

import pytest
from qdrant_client.models import Filter

from server import _build_qdrant_filter


class TestBuildQdrantFilterLayerSupport:
    """Verify layer param is correctly added to Qdrant filter conditions."""

    def test_no_args_returns_none(self):
        assert _build_qdrant_filter(None) is None
        assert _build_qdrant_filter({}) is None
        assert _build_qdrant_filter({}, None) is None

    def test_layer_only(self):
        result = _build_qdrant_filter(None, "L0")
        assert isinstance(result, Filter)
        assert len(result.must) == 1
        assert result.must[0].key == "layer"
        assert result.must[0].match.value == "L0"

    def test_filters_only(self):
        result = _build_qdrant_filter({"project": "acme"})
        assert isinstance(result, Filter)
        assert len(result.must) == 1
        assert result.must[0].key == "project"

    def test_filters_and_layer_combined(self):
        result = _build_qdrant_filter({"project": "acme"}, "L2")
        assert len(result.must) == 2
        keys = {c.key for c in result.must}
        assert keys == {"project", "layer"}

    @pytest.mark.parametrize("layer", ["L0", "L1", "L2"])
    def test_all_layer_values(self, layer):
        result = _build_qdrant_filter(None, layer)
        assert result.must[0].match.value == layer

    def test_empty_string_layer_treated_as_no_layer(self):
        result = _build_qdrant_filter({"project": "x"}, "")
        assert len(result.must) == 1
        assert result.must[0].key == "project"
```

- [ ] **Step 2: Run the tests**

```bash
cd mcp-server && python3 -m pytest tests/test_layer_filter.py -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test: add layer filter tests for _build_qdrant_filter"
```

---

### Task 5: Write new layer generation tests (Ingestion)

**Files:**
- Create: `ingestion/tests/test_layer_generation.py`

Tests for `generate_l0`, `generate_l1`, and L0/L1 integration in `ingest_text_chunks`.

- [ ] **Step 1: Write the test file**

```python
"""Tests for L0/L1 layer generation and ingestion pipeline integration."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ingestion_api
from ingestion_api import generate_l0, generate_l1, ingest_text_chunks


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_globals(monkeypatch):
    """Patch module-level clients so tests never hit real services."""
    monkeypatch.setattr(ingestion_api, "http_client", AsyncMock())
    monkeypatch.setattr(ingestion_api, "qdrant", AsyncMock())
    monkeypatch.setattr(ingestion_api, "pg_pool", AsyncMock())
    monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", True)


@pytest.fixture
def mock_completion(monkeypatch):
    provider = AsyncMock()
    provider.generate = AsyncMock(return_value="Generated text")
    monkeypatch.setattr(ingestion_api, "completion_provider", provider)
    return provider


# ── generate_l0 ──────────────────────────────────────────────

class TestGenerateL0:
    @pytest.mark.asyncio
    async def test_returns_text_on_success(self, mock_completion):
        mock_completion.generate = AsyncMock(return_value="A document about GDPR.")
        result = await generate_l0(["chunk1", "chunk2"], source="test.md")
        assert result == "A document about GDPR."

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", False)
        result = await generate_l0(["chunk"])
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self, mock_completion):
        mock_completion.generate = AsyncMock(side_effect=RuntimeError("LLM down"))
        result = await generate_l0(["chunk"])
        assert result is None

    @pytest.mark.asyncio
    async def test_truncates_long_input(self, mock_completion):
        long_chunk = "x" * 5000
        await generate_l0([long_chunk])
        call_args = mock_completion.generate.call_args
        user_prompt = call_args.kwargs.get("user_prompt", call_args[0][-1] if len(call_args[0]) > 2 else "")
        assert "[truncated]" in user_prompt or len(user_prompt) < 5000


# ── generate_l1 ──────────────────────────────────────────────

class TestGenerateL1:
    @pytest.mark.asyncio
    async def test_returns_text_on_success(self, mock_completion):
        mock_completion.generate = AsyncMock(return_value="# Overview\n- key point")
        result = await generate_l1(["chunk1", "chunk2"])
        assert "Overview" in result

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", False)
        result = await generate_l1(["chunk"])
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self, mock_completion):
        mock_completion.generate = AsyncMock(side_effect=RuntimeError("LLM down"))
        result = await generate_l1(["chunk"])
        assert result is None


# ── Pipeline integration ─────────────────────────────────────

class TestIngestLayerIntegration:
    @pytest.mark.asyncio
    async def test_l2_payloads_have_layer_and_doc_id(self, monkeypatch, mock_completion):
        """Every L2 chunk must include layer='L2' and a doc_id."""
        monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", False)
        mock_embed = AsyncMock(return_value=[0.1] * 768)
        monkeypatch.setattr(ingestion_api, "get_embedding", mock_embed)
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = MagicMock(
            contains_pii=False, entity_counts={}, anonymized_text=None,
        )
        monkeypatch.setattr(ingestion_api, "get_scanner", lambda: mock_scanner)

        await ingest_text_chunks(
            chunks=["hello world"],
            collection="pb_general",
            source="test.md",
            classification="internal",
            project="test",
            metadata={},
        )

        upsert_call = ingestion_api.qdrant.upsert.call_args_list[0]
        points = upsert_call.kwargs.get("points", upsert_call[1].get("points", []))
        for pt in points:
            assert pt.payload["layer"] == "L2"
            assert "doc_id" in pt.payload

    @pytest.mark.asyncio
    async def test_l0_l1_upserted_when_generation_succeeds(self, monkeypatch, mock_completion):
        """When LLM succeeds, L0 and L1 are upserted as separate Qdrant points."""
        mock_completion.generate = AsyncMock(
            side_effect=["L0 abstract text", "# L1 Overview\n- key point"]
        )
        mock_embed = AsyncMock(return_value=[0.1] * 768)
        monkeypatch.setattr(ingestion_api, "get_embedding", mock_embed)
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = MagicMock(
            contains_pii=False, entity_counts={}, anonymized_text=None,
        )
        monkeypatch.setattr(ingestion_api, "get_scanner", lambda: mock_scanner)

        await ingest_text_chunks(
            chunks=["hello world"],
            collection="pb_general",
            source="test.md",
            classification="internal",
            project="test",
            metadata={},
        )

        # 3 upsert calls: L2 batch, L0 single, L1 single
        assert ingestion_api.qdrant.upsert.call_count == 3

        l0_call = ingestion_api.qdrant.upsert.call_args_list[1]
        l0_points = l0_call.kwargs.get("points", l0_call[1].get("points", []))
        assert l0_points[0].payload["layer"] == "L0"
        assert l0_points[0].payload["text"] == "L0 abstract text"

        l1_call = ingestion_api.qdrant.upsert.call_args_list[2]
        l1_points = l1_call.kwargs.get("points", l1_call[1].get("points", []))
        assert l1_points[0].payload["layer"] == "L1"
```

- [ ] **Step 2: Run the tests**

```bash
cd ingestion && python3 -m pytest tests/test_layer_generation.py -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test: add simplified layer generation tests for ingestion pipeline"
```

---

### Task 6: Update documentation

**Files:**
- Modify: `CLAUDE.md:150-157` (Summarization section — remove `SUMMARIZATION_MODEL` mention)
- Modify: `CLAUDE.md:180` (Context Layers feature — remove OPA layer mention)
- Modify: `docs/architektur.md:322-351` (Layer section — remove OPA access control)
- Modify: `.env.example:42` (already done in Task 3)

- [ ] **Step 1: Update CLAUDE.md — remove SUMMARIZATION_MODEL backward-compat line**

Replace line 154:
```
Backward compat: `SUMMARIZATION_MODEL` still read as fallback if `LLM_MODEL` not set.
```
with nothing (delete the line).

- [ ] **Step 2: Update docs/architektur.md — simplify layer section**

Replace the OPA access control part of the layer section (lines ~342-346):

```markdown
**OPA-Zugriffssteuerung** (`pb.layers`):
- Admin: immer L2
- Nicht-Admin + confidential: max. L1
- Nicht-Admin + restricted: max. L0
- Viewer + internal: max. L0
```

with:

```markdown
**Zugriffssteuerung:**
Layers sind ein Progressive-Loading-Mechanismus, keine Sicherheitsschicht.
Die bestehende `pb.access`-Policy kontrolliert ob ein Agent ein Dokument sehen darf.
`pb.summarization` kontrolliert ob Rohtext oder nur Zusammenfassungen erlaubt sind.
Jeder Agent mit Zugriff kann jede Layer-Ebene abfragen.
```

- [ ] **Step 3: Run all existing tests to verify nothing is broken**

```bash
cd mcp-server && python3 -m pytest tests/ -v
cd ../ingestion && python3 -m pytest tests/ -v
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: update layer documentation, remove OPA layer access control references"
```
