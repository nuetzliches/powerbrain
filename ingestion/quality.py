"""Data-quality scoring for ingestion (EU AI Act Art. 10).

Computes a composite quality score 0.0-1.0 from five weighted factors:

- **length** (0.25)              — too-short or pathological-length chunks
- **language_confidence** (0.20) — confidence of language detection
- **pii_density** (0.20)         — high PII ratio lowers the score
- **encoding** (0.15)            — mojibake, control characters
- **metadata_completeness** (0.20) — required metadata per source_type

Used by ``ingestion/ingestion_api.py`` between the PII scan and the
embedding step. The actual enforcement decision is made by the OPA
policy ``pb.ingestion.quality_gate``.

Intentionally dependency-light: language detection is a lightweight
heuristic (character distribution + stop-word ratio), and no new
third-party packages are required. Deployers can replace
``detect_language_confidence`` with ``langdetect`` / ``langid`` if
desired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


# ── Weights (must sum to 1.0) ────────────────────────────────
WEIGHTS = {
    "length":                 0.25,
    "language_confidence":    0.20,
    "pii_density":            0.20,
    "encoding":               0.15,
    "metadata_completeness":  0.20,
}

# ── Length bounds ────────────────────────────────────────────
MIN_CHARS       = 20     # below → 0
OPTIMAL_MIN     = 200
OPTIMAL_MAX     = 5000
MAX_CHARS       = 50000  # above → decays toward 0

# ── Required metadata per source_type ────────────────────────
# source_type → set of required metadata keys. "default" is used as
# a fallback when the source_type is unknown.
REQUIRED_METADATA = {
    "default":   {"source", "classification"},
    "code":      {"source", "classification", "project"},
    "contracts": {"source", "classification", "legal_basis"},
    "docs":      {"source", "classification"},
}

# ── Encoding red flags ───────────────────────────────────────
# Classic mojibake trigraphs and control characters below \x20
# (excluding \t \n \r which are benign).
_MOJIBAKE_MARKERS = ("Ã¤", "Ã¶", "Ã¼", "Ã\x9f", "â\x80\x99", "â\x80\x9c",
                     "â\x80\x9d", "\ufffd")
_BENIGN_CONTROL   = {"\t", "\n", "\r"}


# ── Heuristic language detection ─────────────────────────────
# Very small common-word dictionaries for EN/DE. Returns (lang, score)
# where score ∈ [0.0, 1.0] is the confidence. Deployers can replace
# this with a real library if coverage matters.
_EN_STOPWORDS = {"the", "and", "is", "of", "to", "in", "that", "it",
                 "for", "on", "with", "as", "was", "are", "this", "be",
                 "at", "by", "not", "or", "an"}
_DE_STOPWORDS = {"der", "die", "das", "und", "ist", "ein", "eine",
                 "zu", "im", "mit", "von", "den", "des", "auf", "für",
                 "dem", "es", "sich", "nicht", "auch"}


LANGUAGE_DETECT_SAMPLE_CHARS = 20_000


def detect_language_confidence(text: str) -> tuple[str, float]:
    """Return a ``(lang_hint, confidence)`` pair.

    Confidence is the ratio of detected stop-words to total word count,
    clamped to [0, 1]. Returns ``("unknown", 0.0)`` for texts with no
    word tokens. For very large inputs only the first
    ``LANGUAGE_DETECT_SAMPLE_CHARS`` characters are scanned to keep
    ingestion latency bounded — stop-word distribution is stable on
    representative samples.
    """
    if not text:
        return "unknown", 0.0
    sample = text[:LANGUAGE_DETECT_SAMPLE_CHARS]
    words = [w.lower().strip(".,;:!?\"'()[]") for w in sample.split()]
    words = [w for w in words if w]
    if not words:
        return "unknown", 0.0

    en_hits = sum(1 for w in words if w in _EN_STOPWORDS)
    de_hits = sum(1 for w in words if w in _DE_STOPWORDS)

    best_lang, best_hits = ("en", en_hits) if en_hits >= de_hits else ("de", de_hits)
    if best_hits == 0:
        return "unknown", 0.0
    # Normalise: 1 stop word per 10 tokens is strong signal → cap at 1.0
    confidence = min(1.0, (best_hits / max(len(words), 1)) * 10.0)
    return best_lang, confidence


# ── Individual scoring factors ───────────────────────────────

def score_length(text: str) -> float:
    n = len(text)
    if n < MIN_CHARS:
        return 0.0
    if OPTIMAL_MIN <= n <= OPTIMAL_MAX:
        return 1.0
    if n < OPTIMAL_MIN:
        # Linear ramp from MIN_CHARS→0 to OPTIMAL_MIN→1
        return (n - MIN_CHARS) / (OPTIMAL_MIN - MIN_CHARS)
    if n <= MAX_CHARS:
        # Linear decay from OPTIMAL_MAX→1 to MAX_CHARS→0.2
        span = MAX_CHARS - OPTIMAL_MAX
        return 1.0 - 0.8 * ((n - OPTIMAL_MAX) / span)
    return 0.2


def score_encoding(text: str) -> float:
    if not text:
        return 0.0
    bad = 0
    for marker in _MOJIBAKE_MARKERS:
        bad += text.count(marker)
    control_count = sum(
        1 for ch in text
        if (ch < "\x20" and ch not in _BENIGN_CONTROL) or ch == "\x7f"
    )
    bad += control_count
    if bad == 0:
        return 1.0
    ratio = bad / max(len(text), 1)
    # 1 bad char per 1000 → 0.9; 1 per 100 → 0.0
    return max(0.0, 1.0 - ratio * 100.0)


def score_pii_density(text: str, pii_entity_count: int) -> float:
    """High PII density → lower score. Based on entities per 1000 chars."""
    if not text:
        return 1.0
    density = pii_entity_count / (len(text) / 1000.0 + 1e-9)
    # 0 entities → 1.0; 1/1000 → 0.8; 5/1000 → 0.0
    return max(0.0, 1.0 - density * 0.2)


def score_metadata_completeness(metadata: dict, source_type: str) -> float:
    required = REQUIRED_METADATA.get(source_type, REQUIRED_METADATA["default"])
    if not required:
        return 1.0
    present = sum(1 for k in required if metadata.get(k))
    return present / len(required)


# ── Composite score ──────────────────────────────────────────

@dataclass
class QualityReport:
    score:    float
    factors:  dict[str, float] = field(default_factory=dict)
    language: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "score":    round(self.score, 4),
            "factors":  {k: round(v, 4) for k, v in self.factors.items()},
            "language": self.language,
            "weights":  WEIGHTS,
        }


def compute_quality_score(
    text: str,
    *,
    metadata: dict,
    source_type: str = "default",
    pii_entity_count: int = 0,
) -> QualityReport:
    """Compute a composite quality score for a document.

    ``text`` should be the full document text (or a representative
    concatenation of the chunks). For ingestion we pass the joined
    chunks.
    """
    length    = score_length(text)
    encoding  = score_encoding(text)
    lang, lang_conf = detect_language_confidence(text)
    pii       = score_pii_density(text, pii_entity_count)
    meta      = score_metadata_completeness(metadata, source_type)

    factors = {
        "length":                length,
        "language_confidence":   lang_conf,
        "pii_density":           pii,
        "encoding":              encoding,
        "metadata_completeness": meta,
    }
    composite = sum(factors[k] * WEIGHTS[k] for k in WEIGHTS)
    # Defensive clamp
    composite = max(0.0, min(1.0, composite))

    return QualityReport(score=composite, factors=factors, language=lang)


# ── Duplicate detection ──────────────────────────────────────

def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity for two vectors. Returns 0.0 on zero-vectors
    or dimension mismatch (duplicate-detection semantics: 'no signal'
    means 'not duplicate'). Delegates to ``shared.drift_check``."""
    from shared.drift_check import cosine_similarity as _shared_cosine
    sim = _shared_cosine(a, b)
    return 0.0 if sim is None else sim


def is_duplicate(
    candidate_embedding: list[float],
    reference_embeddings: list[list[float]],
    threshold: float = 0.95,
) -> tuple[bool, float]:
    """Return ``(is_dup, max_similarity)`` for an embedding against a
    reference set.
    """
    best = 0.0
    for ref in reference_embeddings:
        s = cosine_similarity(candidate_embedding, ref)
        if s > best:
            best = s
    return best >= threshold, best
