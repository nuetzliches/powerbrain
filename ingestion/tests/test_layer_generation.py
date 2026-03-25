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
    # Disable embedding cache so tests are deterministic
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    mock_cache.set.return_value = None
    monkeypatch.setattr(ingestion_api, "embedding_cache", mock_cache)


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
        # Mock batch embedding for the new batch path
        mock_embed_batch = AsyncMock(return_value=[[0.1] * 768])
        monkeypatch.setattr(ingestion_api.embedding_provider, "embed_batch", mock_embed_batch)
        mock_scanner = MagicMock()
        mock_scanner.scan_text.return_value = MagicMock(
            contains_pii=False, entity_counts={}, anonymized_text=None,
        )
        monkeypatch.setattr(ingestion_api, "get_scanner", lambda: mock_scanner)
        # Mock OPA privacy check to allow ingestion
        mock_opa_check = AsyncMock(return_value={
            "pii_action": "allow", 
            "dual_storage_enabled": False,
            "retention_days": 365
        })
        monkeypatch.setattr(ingestion_api, "check_opa_privacy", mock_opa_check)

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
        # Mock batch embedding for the new batch path
        mock_embed_batch = AsyncMock(return_value=[[0.1] * 768])
        monkeypatch.setattr(ingestion_api.embedding_provider, "embed_batch", mock_embed_batch)
        mock_scanner = MagicMock()
        mock_scanner.scan_text.return_value = MagicMock(
            contains_pii=False, entity_counts={}, anonymized_text=None,
        )
        monkeypatch.setattr(ingestion_api, "get_scanner", lambda: mock_scanner)
        # Mock OPA privacy check to allow ingestion  
        mock_opa_check = AsyncMock(return_value={
            "pii_action": "allow", 
            "dual_storage_enabled": False,
            "retention_days": 365
        })
        monkeypatch.setattr(ingestion_api, "check_opa_privacy", mock_opa_check)

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