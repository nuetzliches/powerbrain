"""Tests for L0/L1 layer generation and pipeline integration."""

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
    # Default: layer generation enabled
    monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", True)


@pytest.fixture
def mock_completion(monkeypatch):
    """Mock CompletionProvider.generate to return controlled text."""
    provider = AsyncMock()
    provider.generate = AsyncMock(return_value="Generated text")
    monkeypatch.setattr(ingestion_api, "completion_provider", provider)
    return provider


@pytest.fixture
def mock_embedding(monkeypatch):
    """Mock get_embedding to return a dummy vector."""
    embed = AsyncMock(return_value=[0.1] * 768)
    monkeypatch.setattr(ingestion_api, "get_embedding", embed)
    return embed


@pytest.fixture
def mock_scanner(monkeypatch):
    """Mock PII scanner that finds no PII."""
    from pii_scanner import PIIScanResult

    scanner = MagicMock()
    scanner.scan_text.return_value = PIIScanResult()
    monkeypatch.setattr(ingestion_api, "get_scanner", lambda: scanner)
    return scanner


# ── generate_l0 Tests ────────────────────────────────────────

class TestGenerateL0:
    @pytest.mark.asyncio
    async def test_returns_llm_output(self, mock_completion):
        mock_completion.generate.return_value = "A document about GDPR compliance."

        result = await generate_l0(["chunk one", "chunk two"])

        assert result == "A document about GDPR compliance."

    @pytest.mark.asyncio
    async def test_passes_correct_system_prompt(self, mock_completion):
        mock_completion.generate.return_value = "abstract"

        await generate_l0(["text"])

        call_kwargs = mock_completion.generate.call_args[1]
        assert "single-sentence abstract" in call_kwargs["system_prompt"]
        assert "max 100 tokens" in call_kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_joins_chunks_as_user_prompt(self, mock_completion):
        mock_completion.generate.return_value = "abstract"

        await generate_l0(["chunk A", "chunk B"])

        call_kwargs = mock_completion.generate.call_args[1]
        assert "chunk A\n\nchunk B" == call_kwargs["user_prompt"]

    @pytest.mark.asyncio
    async def test_truncates_long_text(self, mock_completion):
        mock_completion.generate.return_value = "abstract"
        long_chunk = "x" * 5000

        await generate_l0([long_chunk])

        call_kwargs = mock_completion.generate.call_args[1]
        assert len(call_kwargs["user_prompt"]) <= 4000 + len("\n\n[truncated]")
        assert call_kwargs["user_prompt"].endswith("[truncated]")

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self, mock_completion):
        mock_completion.generate.side_effect = Exception("LLM timeout")

        result = await generate_l0(["some text"])

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self, monkeypatch, mock_completion):
        monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", False)

        result = await generate_l0(["some text"])

        assert result is None
        mock_completion.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_returns_none(self, mock_completion):
        mock_completion.generate.return_value = None

        result = await generate_l0(["some text"])

        assert result is None


# ── generate_l1 Tests ────────────────────────────────────────

class TestGenerateL1:
    @pytest.mark.asyncio
    async def test_returns_llm_output(self, mock_completion):
        mock_completion.generate.return_value = "# Overview\n- Topic A\n- Topic B"

        result = await generate_l1(["chunk one", "chunk two"])

        assert result == "# Overview\n- Topic A\n- Topic B"

    @pytest.mark.asyncio
    async def test_passes_correct_system_prompt(self, mock_completion):
        mock_completion.generate.return_value = "overview"

        await generate_l1(["text"])

        call_kwargs = mock_completion.generate.call_args[1]
        assert "structured Markdown overview" in call_kwargs["system_prompt"]
        assert "max 500 tokens" in call_kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_truncates_at_8000_chars(self, mock_completion):
        mock_completion.generate.return_value = "overview"
        long_chunk = "y" * 9000

        await generate_l1([long_chunk])

        call_kwargs = mock_completion.generate.call_args[1]
        assert len(call_kwargs["user_prompt"]) <= 8000 + len("\n\n[truncated]")
        assert call_kwargs["user_prompt"].endswith("[truncated]")

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self, mock_completion):
        mock_completion.generate.side_effect = Exception("connection refused")

        result = await generate_l1(["some text"])

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self, monkeypatch, mock_completion):
        monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", False)

        result = await generate_l1(["some text"])

        assert result is None
        mock_completion.generate.assert_not_called()


# ── Pipeline Integration Tests ───────────────────────────────

class TestIngestTextChunksLayerIntegration:
    """Tests that ingest_text_chunks correctly integrates L0/L1 generation."""

    @pytest.mark.asyncio
    async def test_l2_payloads_have_layer_and_doc_id(
        self, mock_embedding, mock_scanner, mock_completion, monkeypatch,
    ):
        """Every L2 chunk payload must include layer='L2' and a doc_id."""
        monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", False)

        result = await ingest_text_chunks(
            chunks=["chunk one", "chunk two"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project="test-project",
            metadata={},
        )

        assert result["status"] == "ok"
        assert result["chunks_ingested"] == 2

        # Check L2 upsert call
        qdrant = ingestion_api.qdrant
        l2_call = qdrant.upsert.call_args_list[0]
        l2_points = l2_call[1]["points"]
        for pt in l2_points:
            assert pt.payload["layer"] == "L2"
            assert "doc_id" in pt.payload
            assert pt.payload["doc_id"]  # non-empty

    @pytest.mark.asyncio
    async def test_l0_l1_generated_and_upserted(
        self, mock_embedding, mock_scanner, mock_completion,
    ):
        """When LLM succeeds, L0 and L1 should be upserted as separate points."""
        mock_completion.generate.side_effect = [
            "L0 abstract text",
            "# L1 Overview\n- key point",
        ]

        result = await ingest_text_chunks(
            chunks=["hello world"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project=None,
            metadata={},
        )

        assert result["l0_point_id"] is not None
        assert result["l1_point_id"] is not None

        # Should have 3 upsert calls: L2, L0, L1
        qdrant = ingestion_api.qdrant
        assert qdrant.upsert.call_count == 3

        # Verify L0 point payload
        l0_call = qdrant.upsert.call_args_list[1]
        l0_points = l0_call[1]["points"]
        assert len(l0_points) == 1
        assert l0_points[0].payload["layer"] == "L0"
        assert l0_points[0].payload["text"] == "L0 abstract text"

        # Verify L1 point payload
        l1_call = qdrant.upsert.call_args_list[2]
        l1_points = l1_call[1]["points"]
        assert len(l1_points) == 1
        assert l1_points[0].payload["layer"] == "L1"
        assert l1_points[0].payload["text"] == "# L1 Overview\n- key point"

    @pytest.mark.asyncio
    async def test_l0_l1_payloads_have_correct_metadata(
        self, mock_embedding, mock_scanner, mock_completion,
    ):
        """L0/L1 points inherit source, classification, project, and custom metadata."""
        mock_completion.generate.side_effect = ["abstract", "overview"]

        await ingest_text_chunks(
            chunks=["content"],
            collection="knowledge_code",
            source="git:repo/file.py",
            classification="confidential",
            project="my-project",
            metadata={"language": "python"},
        )

        qdrant = ingestion_api.qdrant
        for call_idx in (1, 2):  # L0 and L1 calls
            pt = qdrant.upsert.call_args_list[call_idx][1]["points"][0]
            assert pt.payload["source"] == "git:repo/file.py"
            assert pt.payload["classification"] == "confidential"
            assert pt.payload["project"] == "my-project"
            assert pt.payload["language"] == "python"
            assert pt.payload["contains_pii"] is False
            assert "doc_id" in pt.payload

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_llm_failure(
        self, mock_embedding, mock_scanner, mock_completion,
    ):
        """If LLM fails, L2 chunks are still ingested; l0/l1 IDs are None."""
        mock_completion.generate.side_effect = Exception("LLM down")

        result = await ingest_text_chunks(
            chunks=["hello"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project=None,
            metadata={},
        )

        assert result["status"] == "ok"
        assert result["chunks_ingested"] == 1
        assert result["l0_point_id"] is None
        assert result["l1_point_id"] is None

        # Only L2 upsert, no L0/L1
        assert ingestion_api.qdrant.upsert.call_count == 1

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_embedding_failure(
        self, mock_embedding, mock_scanner, mock_completion,
    ):
        """If L0/L1 embedding fails, L2 still succeeds."""
        mock_completion.generate.side_effect = ["abstract", "overview"]

        call_count = 0

        async def embed_with_failure(text):
            nonlocal call_count
            call_count += 1
            # First call(s) succeed (L2 chunks), L0/L1 embedding fails
            if call_count > 1:
                raise Exception("Embedding service down")
            return [0.1] * 768

        mock_embedding.side_effect = embed_with_failure

        result = await ingest_text_chunks(
            chunks=["hello"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project=None,
            metadata={},
        )

        assert result["status"] == "ok"
        assert result["chunks_ingested"] == 1
        # L0 embedding failed
        assert result["l0_point_id"] is None
        # L1 embedding also failed
        assert result["l1_point_id"] is None

    @pytest.mark.asyncio
    async def test_documents_meta_updated_with_layer_ids(
        self, mock_embedding, mock_scanner, mock_completion,
    ):
        """documents_meta UPDATE should include l0_point_id and l1_point_id."""
        mock_completion.generate.side_effect = ["abstract", "overview"]
        pg = ingestion_api.pg_pool

        await ingest_text_chunks(
            chunks=["content"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project=None,
            metadata={},
        )

        # Find the UPDATE call (not INSERT)
        update_call = None
        for call in pg.execute.call_args_list:
            sql = call[0][0]
            if "UPDATE documents_meta" in sql:
                update_call = call
                break

        assert update_call is not None
        sql = update_call[0][0]
        assert "l0_point_id" in sql
        assert "l1_point_id" in sql

        # The 5th and 6th args should be UUIDs
        args = update_call[0]
        l0_id = args[5]  # $5
        l1_id = args[6]  # $6
        assert l0_id is not None
        assert l1_id is not None

    @pytest.mark.asyncio
    async def test_documents_meta_null_ids_when_generation_disabled(
        self, mock_embedding, mock_scanner, mock_completion, monkeypatch,
    ):
        """When layer generation disabled, l0/l1 point IDs should be None in UPDATE."""
        monkeypatch.setattr(ingestion_api, "LAYER_GENERATION_ENABLED", False)
        pg = ingestion_api.pg_pool

        await ingest_text_chunks(
            chunks=["content"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project=None,
            metadata={},
        )

        update_call = None
        for call in pg.execute.call_args_list:
            sql = call[0][0]
            if "UPDATE documents_meta" in sql:
                update_call = call
                break

        assert update_call is not None
        args = update_call[0]
        assert args[5] is None  # l0_point_id
        assert args[6] is None  # l1_point_id

    @pytest.mark.asyncio
    async def test_l0_l1_use_processed_chunks(
        self, mock_embedding, mock_scanner, mock_completion,
    ):
        """L0/L1 should use the post-PII-processing text, not raw input."""
        # This test verifies the processed_chunks variable is used.
        # When no PII, processed text equals original.
        mock_completion.generate.side_effect = ["abstract", "overview"]

        await ingest_text_chunks(
            chunks=["original text"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project=None,
            metadata={},
        )

        # The L0 call should have received the processed text
        l0_call_kwargs = mock_completion.generate.call_args_list[0][1]
        assert "original text" in l0_call_kwargs["user_prompt"]

    @pytest.mark.asyncio
    async def test_no_l0_l1_when_all_chunks_blocked(
        self, mock_embedding, mock_completion, monkeypatch,
    ):
        """If PII blocks ingestion, no L0/L1 should be generated."""
        from pii_scanner import PIIScanResult

        scanner = MagicMock()
        scan_result = PIIScanResult()
        scan_result.contains_pii = True
        scan_result.entity_counts = {"PERSON": 1}
        scan_result.entity_locations = [
            {"type": "PERSON", "start": 0, "end": 4, "score": 0.9}
        ]
        scanner.scan_text.return_value = scan_result
        monkeypatch.setattr(ingestion_api, "get_scanner", lambda: scanner)

        # OPA blocks PII
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "result": {"pii_action": "block", "dual_storage_enabled": False}
        }
        ingestion_api.http_client.post.return_value = mock_resp

        result = await ingest_text_chunks(
            chunks=["John Doe is here"],
            collection="knowledge_general",
            source="test:inline",
            classification="internal",
            project=None,
            metadata={},
        )

        assert result["status"] == "blocked"
        mock_completion.generate.assert_not_called()
