"""Embedding drift detection (EU AI Act Art. 15).

Computes cosine-distance between a fresh document centroid and a
deployment-snapshot baseline stored in the ``embedding_reference_set``
PostgreSQL table. Used by the pb-worker accuracy_metrics job to fire
``RerankerScoreDrift`` / ``QualityDrift`` Prometheus alerts when a
collection's vector distribution shifts beyond its configured threshold.

Pure Python, dependency-free — Powerbrain already vendorizes numpy via
``qdrant_client``, but the worker container should not need it. The math
is trivial enough that a tight loop in CPython is fast enough for
deployment-snapshot sized samples (≤ 1000 vectors).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


__all__ = [
    "DEFAULT_THRESHOLDS",
    "DriftResult",
    "compute_centroid",
    "cosine_distance",
    "cosine_similarity",
    "compute_drift",
]


# Per-collection cosine-distance thresholds. Overridden by data.json
# (data.pb.config.drift.thresholds) at runtime — these defaults are the
# fallback when the OPA data section is missing.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "pb_general": 0.08,
    "pb_code":    0.12,
    "pb_rules":   0.05,
}
DEFAULT_THRESHOLD = 0.10


@dataclass
class DriftResult:
    collection:    str
    distance:      float
    threshold:     float
    drifted:       bool
    sample_count:  int
    reference_dim: int

    def to_dict(self) -> dict:
        return {
            "collection":    self.collection,
            "distance":      round(self.distance, 6),
            "threshold":     round(self.threshold, 6),
            "drifted":       self.drifted,
            "sample_count":  self.sample_count,
            "reference_dim": self.reference_dim,
        }


def compute_centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Mean vector. Empty input → empty list."""
    n = len(vectors)
    if n == 0:
        return []
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        if len(v) != dim:
            raise ValueError(
                f"vector dimension mismatch: expected {dim}, got {len(v)}"
            )
        for i, x in enumerate(v):
            out[i] += float(x)
    return [x / n for x in out]


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float | None:
    """Raw cosine similarity in ``[-1, 1]``.

    Returns ``None`` for empty vectors, dimension mismatch, or
    zero-vectors. Callers wrap this with their own "no signal"
    semantics — drift detection treats None as max distance (1.0),
    duplicate detection treats it as zero similarity.
    """
    a_list = list(a)
    b_list = list(b)
    if not a_list or not b_list or len(a_list) != len(b_list):
        return None
    dot = 0.0
    na  = 0.0
    nb  = 0.0
    for x, y in zip(a_list, b_list):
        dot += x * y
        na  += x * x
        nb  += y * y
    if na == 0.0 or nb == 0.0:
        return None
    sim = dot / ((na ** 0.5) * (nb ** 0.5))
    # Clamp for numerical stability
    return max(-1.0, min(1.0, sim))


def cosine_distance(a: Iterable[float], b: Iterable[float]) -> float:
    """1 − cosine_similarity, clamped to [0, 2]. Returns 1.0 on
    zero-vectors or dimension mismatch (treated as 'no signal')."""
    sim = cosine_similarity(a, b)
    if sim is None:
        return 1.0
    return 1.0 - sim


def compute_drift(
    collection: str,
    fresh_vectors: Sequence[Sequence[float]],
    reference_centroid: Sequence[float],
    *,
    thresholds: dict[str, float] | None = None,
) -> DriftResult:
    """Compute drift for one collection.

    Returns a DriftResult with ``drifted=True`` if the cosine distance
    between the fresh-document centroid and the reference centroid
    exceeds the configured per-collection threshold.

    Threshold lookup is a four-level fallback chain (highest priority
    first):
        1. ``thresholds[collection]`` — explicit per-collection override
           from OPA ``data.pb.config.drift.thresholds``
        2. ``thresholds["default"]`` — explicit default from OPA data
        3. ``DEFAULT_THRESHOLDS[collection]`` — Python module fallback
           if the OPA data is missing entirely
        4. ``DEFAULT_THRESHOLD`` (0.10) — final hard-coded default
    """
    fresh_centroid = compute_centroid(fresh_vectors)
    distance = cosine_distance(fresh_centroid, reference_centroid)
    threshold = (thresholds or {}).get(
        collection,
        (thresholds or {}).get("default", DEFAULT_THRESHOLDS.get(collection, DEFAULT_THRESHOLD)),
    )
    return DriftResult(
        collection=collection,
        distance=distance,
        threshold=threshold,
        drifted=distance > threshold,
        sample_count=len(fresh_vectors),
        reference_dim=len(reference_centroid),
    )
