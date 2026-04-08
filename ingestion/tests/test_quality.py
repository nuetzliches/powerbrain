"""Tests for B-43 Data Quality Scoring (EU AI Act Art. 10).

Covers the pure scorer in ``ingestion/quality.py`` and the OPA quality
gate helper ``check_opa_quality_gate`` in ``ingestion_api.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from quality import (
    MAX_CHARS,
    MIN_CHARS,
    OPTIMAL_MAX,
    OPTIMAL_MIN,
    QualityReport,
    WEIGHTS,
    compute_quality_score,
    cosine_similarity,
    detect_language_confidence,
    is_duplicate,
    score_encoding,
    score_length,
    score_metadata_completeness,
    score_pii_density,
)


# ── Length scoring ───────────────────────────────────────────

class TestScoreLength:
    def test_too_short_zero(self):
        assert score_length("a" * 5) == 0.0

    def test_optimal_range_one(self):
        assert score_length("x" * OPTIMAL_MIN) == 1.0
        assert score_length("x" * 2000) == 1.0
        assert score_length("x" * OPTIMAL_MAX) == 1.0

    def test_below_optimal_linear(self):
        half = (MIN_CHARS + OPTIMAL_MIN) // 2
        s = score_length("x" * half)
        assert 0.0 < s < 1.0

    def test_above_optimal_decays(self):
        s = score_length("x" * (OPTIMAL_MAX + 1000))
        assert 0.2 < s < 1.0

    def test_extreme_length_floor(self):
        assert score_length("x" * (MAX_CHARS + 10000)) == 0.2


# ── Encoding scoring ─────────────────────────────────────────

class TestScoreEncoding:
    def test_clean_text(self):
        assert score_encoding("Hello world, this is fine.") == 1.0

    def test_mojibake_penalised(self):
        # 500-char text with 1 mojibake marker → ~0.8
        text = "Ã¤" + "a" * 500
        s = score_encoding(text)
        assert 0.0 < s < 1.0

    def test_replacement_char_penalised(self):
        text = "\ufffd" + "a" * 100
        assert score_encoding(text) < 1.0

    def test_newlines_are_benign(self):
        assert score_encoding("line 1\nline 2\tok\r\n") == 1.0

    def test_control_chars_penalised(self):
        assert score_encoding("\x01\x02\x03 text") < 1.0


# ── PII density ──────────────────────────────────────────────

class TestScorePiiDensity:
    def test_no_pii(self):
        assert score_pii_density("some clean document", 0) == 1.0

    def test_moderate_pii(self):
        s = score_pii_density("x" * 1000, 1)
        assert 0.7 < s < 0.9

    def test_heavy_pii_zero(self):
        s = score_pii_density("x" * 1000, 30)
        assert s == 0.0


# ── Metadata completeness ────────────────────────────────────

class TestScoreMetadataCompleteness:
    def test_default_complete(self):
        meta = {"source": "foo", "classification": "internal"}
        assert score_metadata_completeness(meta, "default") == 1.0

    def test_default_partial(self):
        meta = {"source": "foo"}
        assert score_metadata_completeness(meta, "default") == 0.5

    def test_code_requires_project(self):
        meta = {"source": "git", "classification": "internal"}
        assert score_metadata_completeness(meta, "code") < 1.0

    def test_contracts_requires_legal_basis(self):
        full = {"source": "s", "classification": "confidential",
                "legal_basis": "contract"}
        assert score_metadata_completeness(full, "contracts") == 1.0

        missing = {"source": "s", "classification": "confidential"}
        assert score_metadata_completeness(missing, "contracts") < 1.0

    def test_unknown_source_type_uses_default(self):
        meta = {"source": "foo", "classification": "internal"}
        assert score_metadata_completeness(meta, "made_up_type") == 1.0


# ── Language detection ───────────────────────────────────────

class TestDetectLanguage:
    def test_english(self):
        lang, conf = detect_language_confidence(
            "The quick brown fox jumps over the lazy dog and it is fine."
        )
        assert lang == "en"
        assert conf > 0.5

    def test_german(self):
        lang, conf = detect_language_confidence(
            "Der schnelle braune Fuchs springt über den faulen Hund und es ist gut."
        )
        assert lang == "de"
        assert conf > 0.3

    def test_unknown(self):
        lang, conf = detect_language_confidence("foo bar baz qux quux")
        assert lang == "unknown"
        assert conf == 0.0

    def test_empty(self):
        lang, conf = detect_language_confidence("")
        assert lang == "unknown"
        assert conf == 0.0


# ── Composite score ──────────────────────────────────────────

class TestComputeQualityScore:
    def test_weights_sum_to_one(self):
        assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

    def test_high_quality_english_doc(self):
        text = (
            "This is a well-formed document with clear content, "
            "proper punctuation, and enough length to pass the length "
            "check comfortably. The content is structured and readable "
            "and it covers the subject in reasonable detail with several "
            "sentences that contain common English stop-words."
        )
        # pad to optimal length
        text = text * 3
        report = compute_quality_score(
            text,
            metadata={"source": "doc", "classification": "internal"},
            source_type="default",
            pii_entity_count=0,
        )
        assert report.score > 0.7
        assert report.language == "en"

    def test_too_short_scores_low(self):
        report = compute_quality_score(
            "short",
            metadata={"source": "s", "classification": "internal"},
            source_type="default",
        )
        assert report.score < 0.6

    def test_missing_metadata_drops_score(self):
        text = "x" * 500
        full = compute_quality_score(
            text,
            metadata={"source": "s", "classification": "internal"},
            source_type="default",
        )
        partial = compute_quality_score(
            text,
            metadata={},
            source_type="default",
        )
        assert full.score > partial.score

    def test_mojibake_drops_score(self):
        text_ok = "valid text " * 30
        text_bad = "Ã¤Ã¶Ã¼ " * 100
        ok = compute_quality_score(
            text_ok,
            metadata={"source": "s", "classification": "internal"},
            source_type="default",
        )
        bad = compute_quality_score(
            text_bad,
            metadata={"source": "s", "classification": "internal"},
            source_type="default",
        )
        assert ok.score > bad.score

    def test_to_dict_shape(self):
        report = compute_quality_score(
            "hello world " * 50,
            metadata={"source": "s", "classification": "internal"},
            source_type="default",
        )
        d = report.to_dict()
        assert "score" in d
        assert "factors" in d
        assert "language" in d
        assert "weights" in d
        assert set(d["factors"].keys()) == set(WEIGHTS.keys())

    def test_score_clamped(self):
        # Any input must yield a score within [0, 1]
        report = compute_quality_score(
            "x",
            metadata={},
            source_type="default",
            pii_entity_count=10000,
        )
        assert 0.0 <= report.score <= 1.0


# ── Duplicate detection ──────────────────────────────────────

class TestDuplicateDetection:
    def test_cosine_identical(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == 1.0

    def test_cosine_orthogonal(self):
        assert cosine_similarity([1, 0, 0], [0, 1, 0]) == 0.0

    def test_cosine_zero_vector(self):
        assert cosine_similarity([0, 0, 0], [1, 0, 0]) == 0.0

    def test_cosine_length_mismatch(self):
        assert cosine_similarity([1, 0], [1, 0, 0]) == 0.0

    def test_is_duplicate_hit(self):
        ref = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        dup, sim = is_duplicate([1.0, 0.0, 0.0], ref, threshold=0.95)
        assert dup is True
        assert sim == 1.0

    def test_is_duplicate_miss(self):
        ref = [[1.0, 0.0, 0.0]]
        dup, sim = is_duplicate([0.0, 1.0, 0.0], ref, threshold=0.95)
        assert dup is False
        assert sim == 0.0

    def test_is_duplicate_empty_reference(self):
        dup, sim = is_duplicate([1.0, 2.0], [], threshold=0.5)
        assert dup is False
        assert sim == 0.0


# ── OPA quality_gate helper (fail-closed behaviour) ─────────

class TestCheckOpaQualityGate:
    async def test_allowed_happy_path(self, monkeypatch):
        import ingestion_api
        mock_http = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "result": {"allowed": True, "min_score": 0.6, "reason": ""}
        }
        mock_http.post.return_value = resp
        monkeypatch.setattr(ingestion_api, "http_client", mock_http)

        result = await ingestion_api.check_opa_quality_gate("code", 0.75)
        assert result["allowed"] is True
        assert result["min_score"] == 0.6

    async def test_denied_propagates(self, monkeypatch):
        import ingestion_api
        mock_http = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "result": {"allowed": False, "min_score": 0.8,
                       "reason": "too low"}
        }
        mock_http.post.return_value = resp
        monkeypatch.setattr(ingestion_api, "http_client", mock_http)

        result = await ingestion_api.check_opa_quality_gate("contracts", 0.5)
        assert result["allowed"] is False
        assert result["min_score"] == 0.8
        assert "too low" in result["reason"]

    async def test_opa_failure_fail_closed(self, monkeypatch):
        import ingestion_api
        mock_http = AsyncMock()
        mock_http.post.side_effect = Exception("opa down")
        monkeypatch.setattr(ingestion_api, "http_client", mock_http)

        result = await ingestion_api.check_opa_quality_gate("default", 0.9)
        # Must NOT silently allow when OPA is down
        assert result["allowed"] is False
        assert "opa_unreachable" in result["reason"]

    async def test_default_source_type(self, monkeypatch):
        import ingestion_api
        mock_http = AsyncMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "result": {"allowed": True, "min_score": 0.6, "reason": ""}
        }
        mock_http.post.return_value = resp
        monkeypatch.setattr(ingestion_api, "http_client", mock_http)

        await ingestion_api.check_opa_quality_gate("", 0.7)
        call_args = mock_http.post.call_args
        payload = call_args[1]["json"]["input"]
        assert payload["source_type"] == "default"
