"""Tests for evaluation metrics (pure functions)."""

import pytest
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
