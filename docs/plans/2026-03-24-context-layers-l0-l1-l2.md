# Context Layers (L0/L1/L2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add pre-computed L0 (Abstract, ~100 tokens) and L1 (Overview, ~1-2k tokens) layers to every ingested document, enabling agents to progressively load context and reduce token consumption by 60-80%.

**Architecture:** At ingestion time, after chunking and embedding L2 (today's chunks), the system generates L0 and L1 via LLM and stores them as additional Qdrant points with a `layer` payload field. Search supports a `layer` parameter to filter by layer. A new `get_document` MCP tool enables drill-down from L0 → L1 → L2. OPA policies can restrict maximum layer depth per classification.

**Tech Stack:** Python 3.12, Qdrant (payload filtering), PostgreSQL (documents_meta), OPA/Rego (layer policies), CompletionProvider (qwen2.5:3b for L0/L1 generation), existing test framework (pytest, pytest-asyncio, respx).

---

## Design Decisions (Agreed)

| Decision | Choice | Rationale |
|---|---|---|
| Storage | Same Qdrant collection + `layer` payload field | No new infrastructure, Qdrant payload filtering is fast |
| Generation timing | Synchronous at ingestion | Everything immediately available, simpler control flow |
| Agent interaction | `layer` param on `search_knowledge` + new `get_document` tool | Backward-compatible (default=L2), explicit drill-down |
| Existing summarization | Coexists | L0/L1 is per-document, summarize is cross-document ad-hoc |
| Migration | Tag existing as L2 + backfill script | Consistent state after migration |

## Data Model

### Qdrant Payload (extended)

```python
payload = {
    "text": "...",                    # chunk text (L2) or abstract/overview text (L0/L1)
    "source": "text:inline",
    "classification": "internal",
    "project": "my-project",
    "chunk_index": 0,                 # L2: position in document; L0/L1: always 0
    "ingested_at": "2026-...",
    "contains_pii": False,
    "vault_ref": None,
    "layer": "L2",                    # NEW: "L0" | "L1" | "L2"
    "doc_id": "uuid-of-document",     # NEW: groups L0+L1+L2 points for the same document
}
```

### documents_meta (extended)

```sql
ALTER TABLE documents_meta ADD COLUMN IF NOT EXISTS l0_point_id UUID;
ALTER TABLE documents_meta ADD COLUMN IF NOT EXISTS l1_point_id UUID;
```

### OPA Policy Input (extended)

```json
{
    "agent_role": "analyst",
    "classification": "confidential",
    "requested_layer": "L2"
}
```

## LLM Prompts

### L0 Generation (Abstract)

```
System: You are a document abstraction engine. Generate a single-sentence abstract
(max 100 tokens) that captures the essence of the document. The abstract must enable
quick relevance assessment. Do not include specific details — only the topic and scope.
Respond with the abstract only, no preamble.

User: Document source: {source}
Classification: {classification}
Full text (from {chunk_count} chunks):

{all_chunks_concatenated}
```

### L1 Generation (Overview)

```
System: You are a document overview engine. Generate a structured Markdown overview
(max 500 tokens) that covers:
1. What this document is about (1 sentence)
2. Key sections/topics as bullet points
3. Most important facts or numbers
4. What kind of detailed information is available in the full document

The overview enables an AI agent to decide whether to load the full document.
Respond with the overview only, no preamble. Use Markdown formatting.

User: Document source: {source}
Classification: {classification}
Full text (from {chunk_count} chunks):

{all_chunks_concatenated}
```

---

## Task 1: DB Migration — Add L0/L1 columns to documents_meta

**Files:**
- Create: `init-db/012_context_layers.sql`

**Step 1: Write the migration**

```sql
-- 012_context_layers.sql: Add context layer support (L0/L1/L2)

-- Track Qdrant point IDs for L0 and L1 layers per document
ALTER TABLE documents_meta ADD COLUMN IF NOT EXISTS l0_point_id UUID;
ALTER TABLE documents_meta ADD COLUMN IF NOT EXISTS l1_point_id UUID;

-- Index for doc_id lookups (used by get_document tool)
-- Note: doc_id is already the PK (id column), no extra index needed.

COMMENT ON COLUMN documents_meta.l0_point_id IS 'Qdrant point ID for L0 abstract';
COMMENT ON COLUMN documents_meta.l1_point_id IS 'Qdrant point ID for L1 overview';
```

**Step 2: Apply migration manually (existing volume)**

Run: `docker exec kb-postgres sh -c "psql -U kb_admin -d knowledgebase -f /docker-entrypoint-initdb.d/012_context_layers.sql"`
Expected: `ALTER TABLE` (twice)

**Step 3: Commit**

```bash
git add init-db/012_context_layers.sql
git commit -m "feat: add L0/L1 point ID columns to documents_meta (012)"
```

---

## Task 2: Layer Generation Functions in Ingestion

**Files:**
- Modify: `ingestion/ingestion_api.py` (add `generate_l0()`, `generate_l1()`)
- Create: `ingestion/tests/test_layer_generation.py`

**Step 1: Write failing tests**

```python
"""Tests for L0/L1 context layer generation."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


class TestGenerateL0:
    """Test L0 (Abstract) generation."""

    @pytest.mark.asyncio
    async def test_returns_abstract_string(self):
        """L0 generation returns a short abstract string."""
        from ingestion_api import generate_l0

        mock_http = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(
            return_value="HR vacation policy covering 30 days annual leave and approval process."
        )

        result = await generate_l0(
            http_client=mock_http,
            llm_provider=mock_provider,
            model="qwen2.5:3b",
            chunks=["Chunk 1 text about vacation...", "Chunk 2 text about approval..."],
            source="hr-vacation-policy",
            classification="internal",
        )

        assert result is not None
        assert len(result) > 0
        assert len(result) < 500  # L0 should be short
        mock_provider.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_graceful_fallback_on_llm_failure(self):
        """L0 returns None if LLM fails (graceful degradation)."""
        from ingestion_api import generate_l0

        mock_http = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(side_effect=Exception("LLM down"))

        result = await generate_l0(
            http_client=mock_http,
            llm_provider=mock_provider,
            model="qwen2.5:3b",
            chunks=["Some text"],
            source="test",
            classification="internal",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_none(self):
        """L0 returns None for empty chunk list."""
        from ingestion_api import generate_l0

        mock_http = AsyncMock()
        mock_provider = MagicMock()

        result = await generate_l0(
            http_client=mock_http,
            llm_provider=mock_provider,
            model="qwen2.5:3b",
            chunks=[],
            source="test",
            classification="internal",
        )

        assert result is None


class TestGenerateL1:
    """Test L1 (Overview) generation."""

    @pytest.mark.asyncio
    async def test_returns_overview_string(self):
        """L1 generation returns a markdown overview."""
        from ingestion_api import generate_l1

        mock_http = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(
            return_value="# Vacation Policy\n- 30 days annual\n- Approval via TeamLead"
        )

        result = await generate_l1(
            http_client=mock_http,
            llm_provider=mock_provider,
            model="qwen2.5:3b",
            chunks=["Chunk 1...", "Chunk 2..."],
            source="hr-vacation-policy",
            classification="internal",
        )

        assert result is not None
        assert len(result) > 0
        mock_provider.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_graceful_fallback_on_llm_failure(self):
        """L1 returns None if LLM fails."""
        from ingestion_api import generate_l1

        mock_http = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(side_effect=Exception("LLM down"))

        result = await generate_l1(
            http_client=mock_http,
            llm_provider=mock_provider,
            model="qwen2.5:3b",
            chunks=["Some text"],
            source="test",
            classification="internal",
        )

        assert result is None
```

**Step 2: Run test to verify it fails**

Run: `docker exec kb-ingestion python -m pytest tests/test_layer_generation.py -v`
Expected: FAIL (ImportError — `generate_l0` and `generate_l1` don't exist yet)

**Step 3: Implement `generate_l0()` and `generate_l1()`**

Add to `ingestion/ingestion_api.py` after the `chunk_text()` function (after line 170):

```python
# ── L0/L1 Context Layer Generation ──────────────────────────────────────────

async def generate_l0(
    http_client: httpx.AsyncClient,
    llm_provider,
    model: str,
    chunks: list[str],
    source: str,
    classification: str,
) -> str | None:
    """Generate L0 abstract (~100 tokens) for a document. Returns None on failure."""
    if not chunks:
        return None

    combined = "\n\n---\n\n".join(f"Chunk {i+1}:\n{c}" for i, c in enumerate(chunks))

    system_prompt = (
        "You are a document abstraction engine. Generate a single-sentence abstract "
        "(max 100 tokens) that captures the essence of the document. The abstract must "
        "enable quick relevance assessment. Do not include specific details — only the "
        "topic and scope. Respond with the abstract only, no preamble."
    )
    user_prompt = (
        f"Document source: {source}\n"
        f"Classification: {classification}\n"
        f"Full text (from {len(chunks)} chunks):\n\n{combined}"
    )

    try:
        return await llm_provider.generate(
            http_client, model=model, system_prompt=system_prompt, user_prompt=user_prompt,
        )
    except Exception as e:
        log.warning(f"L0 generation failed for {source}: {e}")
        return None


async def generate_l1(
    http_client: httpx.AsyncClient,
    llm_provider,
    model: str,
    chunks: list[str],
    source: str,
    classification: str,
) -> str | None:
    """Generate L1 overview (~500 tokens, Markdown) for a document. Returns None on failure."""
    if not chunks:
        return None

    combined = "\n\n---\n\n".join(f"Chunk {i+1}:\n{c}" for i, c in enumerate(chunks))

    system_prompt = (
        "You are a document overview engine. Generate a structured Markdown overview "
        "(max 500 tokens) that covers:\n"
        "1. What this document is about (1 sentence)\n"
        "2. Key sections/topics as bullet points\n"
        "3. Most important facts or numbers\n"
        "4. What kind of detailed information is available in the full document\n\n"
        "The overview enables an AI agent to decide whether to load the full document. "
        "Respond with the overview only, no preamble. Use Markdown formatting."
    )
    user_prompt = (
        f"Document source: {source}\n"
        f"Classification: {classification}\n"
        f"Full text (from {len(chunks)} chunks):\n\n{combined}"
    )

    try:
        return await llm_provider.generate(
            http_client, model=model, system_prompt=system_prompt, user_prompt=user_prompt,
        )
    except Exception as e:
        log.warning(f"L1 generation failed for {source}: {e}")
        return None
```

**Step 4: Run tests to verify they pass**

Run: `docker exec kb-ingestion python -m pytest tests/test_layer_generation.py -v`
Expected: PASS (all 5 tests)

**Step 5: Commit**

```bash
git add ingestion/ingestion_api.py ingestion/tests/test_layer_generation.py
git commit -m "feat: add L0/L1 context layer generation functions"
```

---

## Task 3: Integrate L0/L1 into Ingestion Pipeline

**Files:**
- Modify: `ingestion/ingestion_api.py` — update `ingest_text_chunks()` (lines 287-460)
- Modify: `ingestion/ingestion_api.py` — add `layer` and `doc_id` to Qdrant payload (lines 419-429)

**Step 1: Write failing integration test**

Add to `ingestion/tests/test_layer_generation.py`:

```python
class TestIngestWithLayers:
    """Test that ingestion pipeline stores L0, L1, and L2 points."""

    @pytest.mark.asyncio
    async def test_ingest_stores_all_layers(self):
        """After ingestion, Qdrant should have L0 + L1 + L2 points with matching doc_id."""
        from unittest.mock import patch, AsyncMock, MagicMock
        import ingestion_api

        mock_qdrant = AsyncMock()
        mock_qdrant.upsert = AsyncMock()

        with patch.object(ingestion_api, 'qdrant', mock_qdrant), \
             patch.object(ingestion_api, 'get_embedding', new_callable=AsyncMock,
                          return_value=[0.1] * 768), \
             patch.object(ingestion_api, 'scanner', MagicMock()), \
             patch.object(ingestion_api, 'pg_pool', AsyncMock()), \
             patch.object(ingestion_api, 'generate_l0', new_callable=AsyncMock,
                          return_value="Abstract text"), \
             patch.object(ingestion_api, 'generate_l1', new_callable=AsyncMock,
                          return_value="# Overview"):

            ingestion_api.scanner.scan_text.return_value = MagicMock(
                contains_pii=False, entity_counts={}, entity_locations=[]
            )

            mock_conn = AsyncMock()
            mock_conn.fetchrow = AsyncMock(return_value=None)
            mock_conn.execute = AsyncMock()
            ingestion_api.pg_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            ingestion_api.pg_pool.acquire.return_value.__aexit__ = AsyncMock()

            result = await ingestion_api.ingest_text_chunks(
                chunks=["Hello world"],
                collection="knowledge_general",
                source="test",
                classification="internal",
                project=None,
                metadata={},
            )

            assert mock_qdrant.upsert.called
            upsert_calls = mock_qdrant.upsert.call_args_list
            all_points = []
            for c in upsert_calls:
                all_points.extend(c.kwargs.get('points', c[1].get('points', [])))

            layers = [p.payload.get('layer') for p in all_points]
            assert 'L2' in layers
            assert 'L0' in layers
            assert 'L1' in layers

            doc_ids = {p.payload.get('doc_id') for p in all_points}
            assert len(doc_ids) == 1
            assert None not in doc_ids
```

**Step 2: Run test to verify it fails**

Expected: FAIL (payload has no `layer` or `doc_id` field)

**Step 3: Modify `ingest_text_chunks()`**

Changes to `ingestion/ingestion_api.py`:

1. Add env vars at top of file:

```python
LAYER_GENERATION_ENABLED = os.getenv("LAYER_GENERATION_ENABLED", "true").lower() == "true"
LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b"))
```

2. Add `layer` and `doc_id` to every L2 chunk payload (around line 419-429):

```python
payload = {
    "text": chunk,
    "source": source,
    "classification": classification,
    "project": project or "",
    "chunk_index": i,
    "ingested_at": datetime.now(tz=utc).isoformat(),
    "contains_pii": scan_result.contains_pii,
    "vault_ref": vault_ref,
    "layer": "L2",       # NEW
    "doc_id": doc_id,    # NEW (already exists from line 308)
    **metadata,
}
```

3. After L2 upsert (after line 437), add L0/L1 generation:

```python
l0_point_id = None
l1_point_id = None

if LAYER_GENERATION_ENABLED:
    now_iso = datetime.now(tz=utc).isoformat()
    base_meta = {
        "source": source, "classification": classification,
        "project": project or "", "chunk_index": 0,
        "ingested_at": now_iso, "contains_pii": False,
        "vault_ref": None, "doc_id": doc_id,
    }
    processed_chunks = [p.payload["text"] for p in points]

    l0_text = await generate_l0(
        http_client=http_client, llm_provider=llm_provider,
        model=LLM_MODEL, chunks=processed_chunks,
        source=source, classification=classification,
    )
    if l0_text:
        l0_point_id = str(uuid.uuid4())
        l0_embedding = await get_embedding(l0_text)
        await qdrant.upsert(collection_name=collection, points=[
            PointStruct(id=l0_point_id, vector=l0_embedding,
                        payload={**base_meta, "text": l0_text, "layer": "L0"})
        ])

    l1_text = await generate_l1(
        http_client=http_client, llm_provider=llm_provider,
        model=LLM_MODEL, chunks=processed_chunks,
        source=source, classification=classification,
    )
    if l1_text:
        l1_point_id = str(uuid.uuid4())
        l1_embedding = await get_embedding(l1_text)
        await qdrant.upsert(collection_name=collection, points=[
            PointStruct(id=l1_point_id, vector=l1_embedding,
                        payload={**base_meta, "text": l1_text, "layer": "L1"})
        ])
```

4. Update `documents_meta` UPDATE SQL to include `l0_point_id`, `l1_point_id`.

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add ingestion/ingestion_api.py ingestion/tests/test_layer_generation.py
git commit -m "feat: integrate L0/L1 generation into ingestion pipeline"
```

---

## Task 4: Add `layer` Parameter to `search_knowledge` MCP Tool

**Files:**
- Modify: `mcp-server/server.py` — Tool schema (line ~722), dispatch logic (line ~1003)
- Create: `mcp-server/tests/test_layer_search.py`

**Step 1: Write failing test**

```python
"""Tests for layer-aware search filter building."""

import pytest


class TestBuildQdrantFilter:
    """Test that _build_qdrant_filter correctly handles layer parameter."""

    def test_layer_filter_added(self):
        from server import _build_qdrant_filter
        qfilter = _build_qdrant_filter(filters={"project": "test"}, layer="L0")
        assert qfilter is not None
        assert len(qfilter.must) == 2
        layer_cond = [c for c in qfilter.must if c.key == "layer"][0]
        assert layer_cond.match.value == "L0"

    def test_no_layer_filter_when_none(self):
        from server import _build_qdrant_filter
        qfilter = _build_qdrant_filter(filters={"project": "test"}, layer=None)
        assert qfilter is not None
        assert len(qfilter.must) == 1

    def test_empty_filters_no_layer(self):
        from server import _build_qdrant_filter
        qfilter = _build_qdrant_filter(filters={}, layer=None)
        assert qfilter is None
```

**Step 2: Implement `_build_qdrant_filter()` helper**

Extract existing filter-building code (lines 1013-1016) into a reusable function:

```python
def _build_qdrant_filter(filters: dict | None, layer: str | None = None):
    """Build Qdrant Filter from user filters + optional layer constraint."""
    must_conditions = []
    if filters:
        must_conditions.extend(
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in filters.items()
        )
    if layer:
        must_conditions.append(
            FieldCondition(key="layer", match=MatchValue(value=layer))
        )
    return Filter(must=must_conditions) if must_conditions else None
```

**Step 3: Update Tool schema** — add `layer` property to `search_knowledge` and `get_code_context`.

**Step 4: Update dispatch logic** — use `_build_qdrant_filter()` with `layer` argument.

**Step 5: Run full test suite, verify no regressions**

**Step 6: Commit**

```bash
git add mcp-server/server.py mcp-server/tests/test_layer_search.py
git commit -m "feat: add layer parameter to search_knowledge and get_code_context"
```

---

## Task 5: New MCP Tool `get_document`

**Files:**
- Modify: `mcp-server/server.py` — add Tool definition + dispatch handler
- Create: `mcp-server/tests/test_get_document.py`

**Step 1: Add Tool definition**

```python
Tool(
    name="get_document",
    description="Retrieve a specific document by ID at a given context layer. "
                "Use L0 for abstract (~100 tokens), L1 for overview (~1-2k tokens), "
                "L2 for full content chunks. Enables progressive context loading.",
    inputSchema={
        "type": "object",
        "properties": {
            "doc_id":     {"type": "string",
                           "description": "Document ID (from search results metadata.doc_id)"},
            "layer":      {"type": "string", "enum": ["L0", "L1", "L2"], "default": "L1",
                           "description": "Context layer to retrieve"},
            "collection": {"type": "string", "default": "knowledge_general"},
        },
        "required": ["doc_id"]
    }
),
```

**Step 2: Add dispatch handler**

```python
elif name == "get_document":
    doc_id = arguments["doc_id"]
    layer = arguments.get("layer", "L1")
    collection = arguments.get("collection", "knowledge_general")

    doc_filter = Filter(must=[
        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
        FieldCondition(key="layer", match=MatchValue(value=layer)),
    ])

    points, _ = await qdrant.scroll(
        collection_name=collection, scroll_filter=doc_filter,
        limit=100, with_payload=True,
    )

    if points:
        classification = points[0].payload.get("classification", "internal")
        allowed = await check_opa_policy(agent_role, classification)
        if not allowed:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Access denied by policy", "classification": classification}))]

        # OPA layer restriction
        max_layer = await _check_max_layer(agent_role, classification)
        layer_order = {"L0": 0, "L1": 1, "L2": 2}
        if layer_order.get(layer, 2) > layer_order.get(max_layer, 2):
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Layer {layer} not allowed", "max_layer": max_layer}))]

    if layer == "L2":
        points.sort(key=lambda p: p.payload.get("chunk_index", 0))

    results = [{
        "id": str(p.id),
        "content": p.payload.get("text", ""),
        "layer": p.payload.get("layer"),
        "chunk_index": p.payload.get("chunk_index"),
        "metadata": {k: v for k, v in p.payload.items()
                     if k not in ("text", "content", "layer", "chunk_index")},
    } for p in points]

    response = {"doc_id": doc_id, "layer": layer, "results": results, "total": len(results)}
    return [TextContent(type="text", text=json.dumps(response, indent=2, default=str))]
```

**Step 3: Run tests, commit**

```bash
git add mcp-server/server.py mcp-server/tests/test_get_document.py
git commit -m "feat: add get_document MCP tool for layer drill-down"
```

---

## Task 6: OPA Policy — Layer Access Control

**Files:**
- Create: `opa-policies/kb/layers.rego`
- Create: `opa-policies/kb/test_layers.rego`

**Step 1: Write OPA tests first**

```rego
package kb.layers_test

import rego.v1
import data.kb.layers

test_analyst_l1_confidential_allowed if {
    layers.layer_allowed with input as {
        "agent_role": "analyst", "classification": "confidential", "requested_layer": "L1",
    }
}

test_analyst_l2_confidential_denied if {
    not layers.layer_allowed with input as {
        "agent_role": "analyst", "classification": "confidential", "requested_layer": "L2",
    }
}

test_admin_l2_confidential_allowed if {
    layers.layer_allowed with input as {
        "agent_role": "admin", "classification": "confidential", "requested_layer": "L2",
    }
}

test_viewer_l2_public_allowed if {
    layers.layer_allowed with input as {
        "agent_role": "viewer", "classification": "public", "requested_layer": "L2",
    }
}

test_viewer_l2_internal_denied if {
    not layers.layer_allowed with input as {
        "agent_role": "viewer", "classification": "internal", "requested_layer": "L2",
    }
}

test_max_layer_confidential_analyst if {
    layers.max_layer == "L1" with input as {
        "agent_role": "analyst", "classification": "confidential",
    }
}

test_max_layer_restricted_analyst if {
    layers.max_layer == "L0" with input as {
        "agent_role": "analyst", "classification": "restricted",
    }
}

test_max_layer_public_viewer if {
    layers.max_layer == "L2" with input as {
        "agent_role": "viewer", "classification": "public",
    }
}
```

**Step 2: Write the policy**

```rego
package kb.layers

import rego.v1

default max_layer := "L2"

max_layer := "L2" if { input.agent_role == "admin" }

max_layer := "L1" if {
    input.classification == "confidential"
    input.agent_role != "admin"
}

max_layer := "L0" if {
    input.classification == "restricted"
    input.agent_role != "admin"
}

max_layer := "L0" if {
    input.classification == "internal"
    input.agent_role == "viewer"
}

layer_order := {"L0": 0, "L1": 1, "L2": 2}

default layer_allowed := false

layer_allowed if {
    layer_order[input.requested_layer] <= layer_order[max_layer]
}
```

**Step 3: Run OPA tests**

Run: `MSYS_NO_PATHCONV=1 docker exec kb-opa opa test /policies/kb/ -v`
Expected: All PASS (existing 28 + new 8 = 36)

**Step 4: Commit**

```bash
git add opa-policies/kb/layers.rego opa-policies/kb/test_layers.rego
git commit -m "feat: add OPA layer access control policy (kb.layers)"
```

---

## Task 7: Backfill Script for Existing Data

**Files:**
- Create: `ingestion/backfill_layers.py`

Standalone async script that:
1. Scrolls all Qdrant points without a `layer` field
2. Sets `layer=L2` on them via `set_payload`
3. Groups by `doc_id` (from documents_meta)
4. Generates L0+L1 per document
5. Upserts new L0+L1 points
6. Updates `documents_meta.l0_point_id` and `l1_point_id`

Idempotent — checks for existing L0/L1 before generating. Supports `--dry-run` and `--collection` flags.

**Commit:**

```bash
git add ingestion/backfill_layers.py
git commit -m "feat: add backfill script for L0/L1 layer generation"
```

---

## Task 8: Docker Compose + Environment Variables

**Files:**
- Modify: `docker-compose.yml` — add env vars to ingestion service
- Modify: `.env.example` — document new variables

Add to `kb-ingestion` environment:

```yaml
LAYER_GENERATION_ENABLED: "true"
LLM_PROVIDER_URL: ${LLM_PROVIDER_URL:-http://kb-ollama:11434}
LLM_MODEL: ${LLM_MODEL:-qwen2.5:3b}
```

**Commit:**

```bash
git add docker-compose.yml .env.example
git commit -m "feat: add layer generation config to docker-compose"
```

---

## Task 9: Update Documentation

**Files:**
- Modify: `CLAUDE.md` — add `get_document` to MCP Tools, add to Completed Features
- Modify: `docs/architektur.md` — add Context Layers section

**Commit:**

```bash
git add CLAUDE.md docs/architektur.md
git commit -m "docs: document L0/L1/L2 context layers feature"
```

---

## Task 10: E2E Verification

**Step 1:** Pull LLM model: `docker exec kb-ollama ollama pull qwen2.5:3b`

**Step 2:** Rebuild + restart: `docker compose build kb-ingestion kb-mcp-server && docker compose up -d`

**Step 3:** Apply migration: `docker exec kb-postgres psql -U kb_admin -d knowledgebase -f /docker-entrypoint-initdb.d/012_context_layers.sql`

**Step 4:** Ingest test document and verify L0/L1/L2 via MCP search with `layer` parameter

**Step 5:** Test `get_document` drill-down: L0 → L1 → L2

**Step 6:** Run all test suites:
- `python -m pytest mcp-server/tests/ -v`
- `docker exec kb-ingestion python -m pytest tests/ -v`
- `MSYS_NO_PATHCONV=1 docker exec kb-opa opa test /policies/kb/ -v`

**Step 7:** Verify OPA layer restriction (analyst cannot access L2 for confidential)

---

## Summary

| Task | Description | Est. Time |
|---|---|---|
| 1 | DB Migration (012) | 5 min |
| 2 | Layer generation functions + tests | 15 min |
| 3 | Integrate into ingestion pipeline | 20 min |
| 4 | `layer` parameter on search_knowledge | 15 min |
| 5 | New `get_document` MCP tool | 15 min |
| 6 | OPA layer access control policy | 10 min |
| 7 | Backfill script for existing data | 15 min |
| 8 | Docker Compose + env vars | 5 min |
| 9 | Documentation | 10 min |
| 10 | E2E verification | 15 min |
| **Total** | | **~2 hours** |
