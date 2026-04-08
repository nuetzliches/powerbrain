"""Tests for shared/drift_check.py — embedding drift detection (B-45)."""

import math

import pytest

from shared.drift_check import (
    DEFAULT_THRESHOLDS,
    DEFAULT_THRESHOLD,
    compute_centroid,
    compute_drift,
    cosine_distance,
)


# ── compute_centroid ───────────────────────────────────────

class TestComputeCentroid:
    def test_empty(self):
        assert compute_centroid([]) == []

    def test_single_vector(self):
        assert compute_centroid([[1.0, 2.0, 3.0]]) == [1.0, 2.0, 3.0]

    def test_average_of_three(self):
        c = compute_centroid([[1.0, 0.0], [3.0, 0.0], [5.0, 0.0]])
        assert c == [3.0, 0.0]

    def test_dim_mismatch_raises(self):
        with pytest.raises(ValueError):
            compute_centroid([[1.0, 2.0], [3.0]])


# ── cosine_distance ────────────────────────────────────────

class TestCosineDistance:
    def test_identical(self):
        assert cosine_distance([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_orthogonal(self):
        d = cosine_distance([1.0, 0.0], [0.0, 1.0])
        assert d == pytest.approx(1.0, abs=1e-9)

    def test_opposite(self):
        d = cosine_distance([1.0, 0.0], [-1.0, 0.0])
        assert d == pytest.approx(2.0, abs=1e-9)

    def test_zero_vector_returns_one(self):
        assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 1.0

    def test_dim_mismatch_returns_one(self):
        assert cosine_distance([1.0, 0.0], [1.0, 0.0, 0.0]) == 1.0

    def test_empty_returns_one(self):
        assert cosine_distance([], []) == 1.0

    def test_numerical_clamp(self):
        # Slightly noisy vectors should not return negative distances
        d = cosine_distance([0.9999, 0.0001], [0.9999, 0.0001])
        assert d >= 0.0
        assert d <= 2.0


# ── compute_drift ──────────────────────────────────────────

class TestComputeDrift:
    def test_identical_no_drift(self):
        baseline = [1.0, 0.0, 0.0]
        fresh = [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
        result = compute_drift("pb_general", fresh, baseline)
        assert result.drifted is False
        assert result.distance == pytest.approx(0.0, abs=1e-9)
        assert result.sample_count == 2
        assert result.reference_dim == 3

    def test_orthogonal_drift_detected(self):
        baseline = [1.0, 0.0, 0.0]
        fresh = [[0.0, 1.0, 0.0]] * 5
        result = compute_drift("pb_general", fresh, baseline)
        assert result.drifted is True
        assert result.distance == pytest.approx(1.0, abs=1e-9)

    def test_per_collection_threshold(self):
        # pb_code has a higher (more permissive) threshold (0.12) so a
        # small distance is OK there but would alert on pb_rules (0.05)
        baseline = [1.0, 0.0]
        # Construct a fresh centroid with a known small drift
        fresh = [[0.99, 0.14]]  # cosine distance ~ 0.01
        thresholds = {"pb_code": 0.12, "pb_rules": 0.005, "default": 0.10}
        r_code  = compute_drift("pb_code",  fresh, baseline, thresholds=thresholds)
        r_rules = compute_drift("pb_rules", fresh, baseline, thresholds=thresholds)
        assert r_code.drifted is False
        assert r_rules.drifted is True

    def test_unknown_collection_uses_default(self):
        baseline = [1.0, 0.0]
        fresh = [[0.0, 1.0]]
        thresholds = {"default": 0.5}
        r = compute_drift("unknown_xyz", fresh, baseline, thresholds=thresholds)
        assert r.threshold == 0.5
        assert r.drifted is True

    def test_no_thresholds_uses_module_defaults(self):
        baseline = [1.0, 0.0]
        fresh = [[1.0, 0.0]]
        r = compute_drift("pb_general", fresh, baseline)
        assert r.threshold == DEFAULT_THRESHOLDS["pb_general"]

    def test_to_dict_shape(self):
        r = compute_drift("pb_general", [[1.0, 0.0]], [1.0, 0.0])
        d = r.to_dict()
        assert set(d.keys()) == {
            "collection", "distance", "threshold", "drifted",
            "sample_count", "reference_dim",
        }
        assert isinstance(d["distance"], float)
        assert isinstance(d["drifted"], bool)
