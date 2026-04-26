"""Tests for the semantic PII verifier provider.

The provider sits between Presidio's ``scan_text`` output and
downstream consumers (ingestion pipeline, /preview, vault storage).
It's the precision layer that catches Presidio's well-known German
false-positive problem (capitalised nouns like "Zahlungsstatus" or
"Geschäftsführer" flagged as PERSON / LOCATION).

These tests cover the abstraction (factory, noop default, fail-open
contract) and the LLM backend's prompt + parser behaviour. No live
LLM call is made — responses are mocked.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.pii_verify_provider import (
    PIICandidate,
    NoopPIIVerifyProvider,
    LLMPIIVerifyProvider,
    create_pii_verify_provider,
    build_candidates_from_locations,
    apply_verdicts_to_scan_result,
    _parse_verdicts,
    CONTEXT_WINDOW,
    PATTERN_TYPES,
)


# ── Factory ────────────────────────────────────────────────────


class TestFactory:
    def test_default_is_noop(self):
        provider = create_pii_verify_provider()
        assert isinstance(provider, NoopPIIVerifyProvider)
        assert provider.backend_name == "noop"

    def test_unknown_backend_falls_back_to_noop(self):
        # A typo in the env config should not crash ingestion on boot.
        provider = create_pii_verify_provider(backend="gemini-hallucinated")
        assert isinstance(provider, NoopPIIVerifyProvider)

    def test_llm_without_url_or_model_falls_back(self):
        """Misconfigured LLM backend → noop + warning, not a crash."""
        provider = create_pii_verify_provider(backend="llm")
        assert isinstance(provider, NoopPIIVerifyProvider)

        provider = create_pii_verify_provider(backend="llm", base_url="x")
        assert isinstance(provider, NoopPIIVerifyProvider)

        provider = create_pii_verify_provider(backend="llm", model="m")
        assert isinstance(provider, NoopPIIVerifyProvider)

    def test_llm_with_full_config(self):
        provider = create_pii_verify_provider(
            backend="llm", base_url="http://ollama", model="qwen2.5:3b",
        )
        assert isinstance(provider, LLMPIIVerifyProvider)
        assert provider.backend_name == "llm"
        assert provider.model == "qwen2.5:3b"


# ── Noop provider ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestNoopProvider:
    async def test_keeps_everything(self):
        provider = NoopPIIVerifyProvider()
        candidates = [
            PIICandidate("PERSON", "Anna", 0, 4, 0.9, ""),
            PIICandidate("LOCATION", "Berlin", 10, 16, 0.8, ""),
        ]
        http = AsyncMock()
        keep, stats = await provider.verify(http, "Anna lebt in Berlin.", candidates)

        assert keep == [True, True]
        # Noop → everything "forwarded" (pattern types) because subclass hook
        # returns [True, True] by default. Stats.reviewed stays 0 for
        # pattern types and non-zero for ambiguous ones with keep=True.
        assert stats.input_count == 2

    async def test_empty_candidates(self):
        provider = NoopPIIVerifyProvider()
        http = AsyncMock()
        keep, stats = await provider.verify(http, "hallo", [])
        assert keep == []
        assert stats.input_count == 0
        assert stats.reviewed == 0


# ── Pattern vs ambiguous split (structure test) ────────────────


@pytest.mark.asyncio
class TestPatternSplit:
    """Only PERSON/LOCATION/ORGANIZATION hit the LLM. IBAN etc. are
    forwarded automatically."""

    async def test_pattern_types_are_forwarded_without_llm(self):
        # Construct a provider whose subclass hook would *fail* the test
        # if invoked — proves pattern types never leave the base class.
        class ExplodingLLM(LLMPIIVerifyProvider):
            async def _verify_ambiguous(self, http, text, ambiguous):
                raise AssertionError("LLM must not be called for pattern types")

        provider = ExplodingLLM(
            base_url="http://ignored", model="m",
        )
        http = AsyncMock()

        candidates = [
            PIICandidate("IBAN_CODE", "DE89...", 0, 27, 0.95, ""),
            PIICandidate("EMAIL_ADDRESS", "a@b.de", 40, 46, 0.95, ""),
            PIICandidate("PHONE_NUMBER", "+4930", 50, 55, 0.85, ""),
        ]
        keep, stats = await provider.verify(http, "irrelevant", candidates)
        assert keep == [True, True, True]
        assert stats.forwarded == 3
        assert stats.reviewed == 0

    async def test_pattern_types_constant_covers_critical_set(self):
        """Documented contract: we never second-guess these."""
        for pattern in ("IBAN_CODE", "EMAIL_ADDRESS", "PHONE_NUMBER",
                        "DE_TAX_ID", "DE_SOCIAL_SECURITY",
                        "DE_DATE_OF_BIRTH"):
            assert pattern in PATTERN_TYPES


# ── LLM backend ────────────────────────────────────────────────


def _llm_response(verdicts: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = lambda: None
    r.json = lambda: {
        "choices": [{"message": {"content": json.dumps(verdicts)}}]
    }
    return r


@pytest.mark.asyncio
class TestLLMProvider:
    async def test_drops_false_positive(self):
        provider = LLMPIIVerifyProvider(
            base_url="http://ollama:11434", model="qwen2.5:3b",
        )
        http = AsyncMock()
        http.post = AsyncMock(return_value=_llm_response({"0": True, "1": False}))

        candidates = [
            PIICandidate("PERSON", "Anna Müller", 10, 21, 0.9,
                         "Kunde [[Anna Müller]] hat …"),
            PIICandidate("LOCATION", "Geschäftsführer", 30, 45, 0.85,
                         "durch [[Geschäftsführer]] Termin"),
        ]
        keep, stats = await provider.verify(http, "Kunde Anna Müller …", candidates)

        assert keep == [True, False]
        assert stats.reviewed == 2
        assert stats.kept == 1
        assert stats.reverted == 1
        # Per-entity-type accounting
        assert stats.by_entity_type["PERSON"]["kept"] == 1
        assert stats.by_entity_type["LOCATION"]["reverted"] == 1

    async def test_fails_open_on_llm_error(self):
        """Recall always wins over precision. GDPR > demo aesthetics."""
        provider = LLMPIIVerifyProvider(
            base_url="http://ollama", model="qwen2.5:3b",
        )
        http = AsyncMock()
        http.post = AsyncMock(side_effect=Exception("ollama down"))

        candidates = [
            PIICandidate("PERSON", "Anna", 0, 4, 0.9, ""),
        ]
        keep, stats = await provider.verify(http, "Anna lebt …", candidates)

        assert keep == [True]
        assert stats.errors == 1

    async def test_hydrates_context_when_missing(self):
        """Caller may pass empty context; provider fills from surrounding text."""
        provider = LLMPIIVerifyProvider(base_url="u", model="m")
        http = AsyncMock()
        captured_prompt: list[str] = []

        def _capture(url, headers=None, json=None, timeout=None):
            captured_prompt.append(json["messages"][1]["content"])
            return _llm_response({"0": True})
        http.post = AsyncMock(side_effect=_capture)

        text = "Kunde Anna Müller hat angerufen."
        candidates = [
            PIICandidate("PERSON", "Anna Müller", 6, 17, 0.9, ""),
        ]
        await provider.verify(http, text, candidates)
        # Context was hydrated from the raw text, with the hit bracketed.
        assert "[[Anna Müller]]" in captured_prompt[0]

    async def test_malformed_json_is_fail_open(self):
        provider = LLMPIIVerifyProvider(base_url="u", model="m")
        http = AsyncMock()
        bad = MagicMock()
        bad.raise_for_status = lambda: None
        bad.json = lambda: {"choices": [{"message": {"content": "not json"}}]}
        http.post = AsyncMock(return_value=bad)

        candidates = [PIICandidate("PERSON", "Anna", 0, 4, 0.9, "")]
        keep, stats = await provider.verify(http, "Anna lebt", candidates)
        assert keep == [True]
        assert stats.errors == 1


# ── Parser ─────────────────────────────────────────────────────


class TestParseVerdicts:
    def test_basic(self):
        out = _parse_verdicts('{"0": true, "1": false}', 2)
        assert out == [True, False]

    def test_markdown_fence_tolerant(self):
        """Small local LLMs sometimes wrap JSON in ```...``` fences."""
        out = _parse_verdicts('```json\n{"0": true}\n```', 1)
        assert out == [True]

    def test_missing_indices_default_to_true(self):
        # If the LLM skips index 1 we keep it — fail-open.
        out = _parse_verdicts('{"0": false}', 3)
        assert out == [False, True, True]

    def test_out_of_range_indices_ignored(self):
        out = _parse_verdicts('{"99": false, "0": false}', 1)
        assert out == [False]

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError):
            _parse_verdicts("the LLM forgot the json", 1)

    def test_raises_on_non_object(self):
        with pytest.raises(ValueError):
            _parse_verdicts("[true, false]", 2)


# ── Helpers used by ingestion ──────────────────────────────────


class TestBuildCandidates:
    def test_extracts_context_window(self):
        text = "A" * 80 + "Anna" + "B" * 80
        locations = [{"type": "PERSON", "start": 80, "end": 84, "score": 0.9}]
        out = build_candidates_from_locations(text, locations)
        assert len(out) == 1
        assert out[0].text == "Anna"
        assert "[[Anna]]" in out[0].context
        assert len(out[0].context) <= 2 * CONTEXT_WINDOW + len("[[Anna]]") + 10

    def test_empty_locations(self):
        assert build_candidates_from_locations("text", []) == []


class TestApplyVerdicts:
    def test_drops_non_kept(self):
        counts = {"PERSON": 2}
        locations = [
            {"type": "PERSON", "start": 0, "end": 4, "score": 0.9},
            {"type": "PERSON", "start": 10, "end": 20, "score": 0.8},
        ]
        contains, new_counts, new_locs = apply_verdicts_to_scan_result(
            counts, locations, [True, False],
        )
        assert contains is True
        assert new_counts == {"PERSON": 1}
        assert len(new_locs) == 1

    def test_empty_after_filter(self):
        counts = {"PERSON": 1}
        locations = [{"type": "PERSON", "start": 0, "end": 4, "score": 0.9}]
        contains, new_counts, new_locs = apply_verdicts_to_scan_result(
            counts, locations, [False],
        )
        assert contains is False
        assert new_counts == {}
        assert new_locs == []

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            apply_verdicts_to_scan_result({"X": 1}, [{"type": "X"}], [True, True])
