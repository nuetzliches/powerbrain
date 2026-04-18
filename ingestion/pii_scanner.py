"""
PII Scanner for the Ingestion Pipeline
========================================
Detects personally identifiable information in incoming documents and datasets.
Uses Microsoft Presidio (open source) for NER-based detection
and adds regex-based patterns for DE-specific formats.

Configuration via pii_config.yaml (path via PII_CONFIG_PATH env var).

Dependencies (requirements.txt):
  presidio-analyzer
  presidio-anonymizer
  spacy
  pydantic
  pyyaml
  # After installation: python -m spacy download de_core_news_md
"""

import os
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
# OperatorConfig no longer needed — pseudonymize_text does manual replacement

log = logging.getLogger("pb-pii")


# ── Pydantic Config Models ─────────────────────────────────


class PatternConfig(BaseModel):
    """A single regex pattern for a custom recognizer."""
    name: str
    regex: str
    score: float = Field(0.6, ge=0.0, le=1.0)


class RecognizerConfig(BaseModel):
    """A custom regex-based entity recognizer."""
    name: str
    entity_type: str
    language: str = "de"
    patterns: list[PatternConfig]


class LanguageConfig(BaseModel):
    """NLP language with its spaCy model."""
    code: str
    model: str


class PIIScannerConfig(BaseModel):
    """Top-level PII scanner configuration."""
    min_confidence: float = Field(0.7, ge=0.0, le=1.0)
    languages: list[LanguageConfig] = [
        LanguageConfig(code="de", model="de_core_news_md"),
        LanguageConfig(code="en", model="en_core_web_lg"),
    ]
    entity_types: list[str] = [
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE",
        "CREDIT_CARD", "IP_ADDRESS", "LOCATION", "DATE_OF_BIRTH",
    ]
    custom_recognizers: list[RecognizerConfig] = []

    @property
    def all_entity_types(self) -> list[str]:
        """Return deduplicated entity types including custom recognizers."""
        return list(dict.fromkeys(
            self.entity_types + [r.entity_type for r in self.custom_recognizers]
        ))


# ── Config Loading ──────────────────────────────────────────


def load_config(path: str | Path | None = None) -> PIIScannerConfig:
    """Load PII scanner config from YAML file.

    Resolution order:
    1. Explicit *path* argument
    2. ``PII_CONFIG_PATH`` environment variable
    3. ``pii_config.yaml`` next to this file
    """
    if path is None:
        path = os.environ.get(
            "PII_CONFIG_PATH",
            str(Path(__file__).parent / "pii_config.yaml"),
        )
    config_path = Path(path)
    if not config_path.exists():
        log.warning("PII config file not found at %s, using defaults", config_path)
        return PIIScannerConfig()
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
        return PIIScannerConfig(**data)
    except (yaml.YAMLError, Exception) as exc:
        log.warning("Failed to load PII config from %s: %s, using defaults", config_path, exc)
        return PIIScannerConfig()


# ── Scan Result ────────────────────────────────────────────

@dataclass
class PIIScanResult:
    """Result of a PII scan."""
    contains_pii: bool = False
    entity_counts: dict[str, int] = field(default_factory=dict)
    entity_locations: list[dict[str, Any]] = field(default_factory=list)
    pii_fields: list[str] = field(default_factory=list)
    anonymized_text: str | None = None
    pseudonym_map: dict[str, str] = field(default_factory=dict)


# ── Span helpers ───────────────────────────────────────────

def _resolve_overlapping_spans(results: list[Any]) -> list[Any]:
    """Pick at most one result per text span so downstream replacement is safe.

    Presidio readily emits overlapping hits — classic example is the tail
    of a German IBAN (``0550 1234 5678 90``) which the phone-number
    recognizer also matches. If we replace both, the second substitution
    lands in the middle of the first tag and corrupts the output.

    Resolution strategy, applied greedily in descending priority order:

    1. **Higher score wins.** Presidio scores are a calibrated signal —
       the IBAN custom recognizer scored 0.95 outranks the phone regex's
       0.85, which is the desired behaviour on the shared digit span.
    2. **Longer span wins** on score tie. Prefer the hit that covers
       more characters; it's typically the superset.
    3. **Earlier start wins** as a final tie-breaker so the result is
       deterministic across Presidio runs.

    Returns a list of kept results, ordered as they came in (sort-stable).
    """
    if not results:
        return []

    ranked = sorted(
        results,
        key=lambda r: (
            -float(getattr(r, "score", 0.0)),
            -(int(r.end) - int(r.start)),
            int(r.start),
        ),
    )

    kept: list[Any] = []
    for candidate in ranked:
        c_start, c_end = int(candidate.start), int(candidate.end)
        overlaps_kept = False
        for k in kept:
            k_start, k_end = int(k.start), int(k.end)
            if c_start < k_end and k_start < c_end:
                overlaps_kept = True
                break
        if not overlaps_kept:
            kept.append(candidate)

    # Preserve caller-visible order (ascending start). Downstream callers
    # (scan_text, pseudonymize_text) resort anyway, but stable input is
    # easier to reason about in tests.
    kept.sort(key=lambda r: int(r.start))
    return kept


# ── Scanner ─────────────────────────────────────────────────

class PIIScanner:
    """
    Scans text and structured data for personally identifiable information.

    Usage:
        scanner = PIIScanner()

        # Scan free text
        result = scanner.scan_text("Contact: Max Mustermann, max@firma.de")

        # Structured data (e.g. CSV row as dict)
        result = scanner.scan_record({"name": "Max Mustermann", "email": "max@firma.de"})

        # Mask text
        masked = scanner.mask_text("Call 0171-1234567")
        # → "Call <PHONE_NUMBER>"

        # Pseudonymize text (reversible with salt)
        pseudo, mapping = scanner.pseudonymize_text("Max Mustermann ordered", salt="project-key")
        # → pseudo = "a3f8c1d2 ordered", mapping = {"Max Mustermann": "a3f8c1d2"}
    """

    def __init__(
        self,
        languages: list[str] | None = None,
        *,
        config: PIIScannerConfig | None = None,
    ):
        if config is not None:
            self.config = config
        elif languages is not None:
            # Legacy path: build config from languages parameter for backward compat
            self.config = PIIScannerConfig(
                languages=[
                    LanguageConfig(code=lang, model={
                        "de": "de_core_news_md",
                        "en": "en_core_web_lg",
                    }.get(lang, f"{lang}_core_news_md"))
                    for lang in languages
                ],
            )
        else:
            self.config = PIIScannerConfig()

        self.languages = [lang.code for lang in self.config.languages]

        # Configure Presidio NLP engine with spaCy models for each language
        nlp_config = {
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": lang.code, "model_name": lang.model}
                for lang in self.config.languages
            ],
        }
        provider = NlpEngineProvider(nlp_configuration=nlp_config)
        nlp_engine = provider.create_engine()

        self.analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=self.languages,
        )
        self.anonymizer = AnonymizerEngine()

        # Register custom recognizers from config
        for rec_cfg in self.config.custom_recognizers:
            patterns = [
                Pattern(name=p.name, regex=p.regex, score=p.score)
                for p in rec_cfg.patterns
            ]
            recognizer = PatternRecognizer(
                supported_entity=rec_cfg.entity_type,
                name=rec_cfg.name,
                patterns=patterns,
                supported_language=rec_cfg.language,
            )
            self.analyzer.registry.add_recognizer(recognizer)

    def scan_text(self, text: str, language: str = "de") -> PIIScanResult:
        """Scans free text for PII."""
        if not text or not text.strip():
            return PIIScanResult()

        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )

        if not results:
            return PIIScanResult()

        # Collapse overlapping hits so downstream consumers (the vault
        # entity-matching hash lookup in particular) never see two rows
        # covering the same character range.
        kept = _resolve_overlapping_spans(results)

        entity_counts: dict[str, int] = {}
        entity_locations = []

        for r in kept:
            entity_counts[r.entity_type] = entity_counts.get(r.entity_type, 0) + 1
            entity_locations.append({
                "type": r.entity_type,
                "start": r.start,
                "end": r.end,
                "score": round(r.score, 3),
                "text_snippet": text[max(0, r.start - 10):r.end + 10],
            })

        return PIIScanResult(
            contains_pii=True,
            entity_counts=entity_counts,
            entity_locations=entity_locations,
        )

    def scan_record(self, record: dict[str, Any], language: str = "de") -> PIIScanResult:
        """Scans a structured data row (e.g. CSV row) field by field."""
        combined_result = PIIScanResult()
        pii_fields = []

        for field_name, value in record.items():
            if not isinstance(value, str) or not value.strip():
                continue

            field_result = self.scan_text(value, language)
            if field_result.contains_pii:
                combined_result.contains_pii = True
                pii_fields.append(field_name)
                for entity_type, count in field_result.entity_counts.items():
                    combined_result.entity_counts[entity_type] = (
                        combined_result.entity_counts.get(entity_type, 0) + count
                    )
                combined_result.entity_locations.extend(field_result.entity_locations)

        combined_result.pii_fields = pii_fields
        return combined_result

    def mask_text(self, text: str, language: str = "de") -> str:
        """Replaces PII with placeholders: 'Max Mustermann' → '<PERSON>'."""
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )

        # Reconcile overlapping hits the same way the pseudonymiser does
        # so consumers get consistent ``<TYPE>`` placeholders regardless
        # of which path ran. Presidio's default anonymizer splits on
        # overlaps and produces "<DE_DATE_OF_BIRTH><LOCATION>" for the
        # adjacent DOB/LOCATION pair spaCy emits on dotted dates.
        kept = _resolve_overlapping_spans(results)

        anonymized = self.anonymizer.anonymize(
            text=text,
            analyzer_results=kept,
        )
        return anonymized.text

    def pseudonymize_text(
        self, text: str, salt: str, language: str = "de"
    ) -> tuple[str, dict[str, str]]:
        """
        Replaces PII with deterministic pseudonyms.
        Same input + salt → same pseudonym (for linkability).

        Overlapping Presidio hits are resolved before replacement (highest
        score wins; ties go to the longer span). Without this, two hits
        like ``IBAN_CODE[161..188]`` and ``PHONE_NUMBER[171..188]`` both
        get substituted in descending-position order, which leaves the
        PHONE tag pointing into the middle of the freshly-inserted IBAN
        tag and produces artefacts like ``[IBAN_CODE:abc]db0d4]``.

        Returns:
            Tuple of (pseudonymized text, mapping {original → pseudonym})
        """
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )

        kept = _resolve_overlapping_spans(results)

        def make_pseudonym(entity_text: str, entity_type: str) -> str:
            h = hashlib.sha256(f"{salt}:{entity_text}".encode()).hexdigest()[:8]
            return f"[{entity_type}:{h}]"

        # Build individual pseudonyms per kept result (not per entity type),
        # so that multiple entities of the same type get different pseudonyms.
        mapping: dict[str, str] = {}
        for r in kept:
            original = text[r.start:r.end]
            pseudo = make_pseudonym(original, r.entity_type)
            mapping[original] = pseudo

        # Manual replacement (position-descending is safe now that spans
        # don't overlap). We index into the original ``text`` slice for
        # ``original`` — using the mutating ``pseudonymized`` would still
        # work but is needlessly fragile.
        pseudonymized = text
        for r in sorted(kept, key=lambda x: x.start, reverse=True):
            original = text[r.start:r.end]
            pseudo = mapping.get(original, make_pseudonym(original, r.entity_type))
            pseudonymized = pseudonymized[:r.start] + pseudo + pseudonymized[r.end:]

        return pseudonymized, mapping

    def mask_record(self, record: dict[str, Any], language: str = "de") -> dict[str, Any]:
        """Masks PII in all string fields of a record."""
        masked = {}
        for key, value in record.items():
            if isinstance(value, str) and value.strip():
                masked[key] = self.mask_text(value, language)
            else:
                masked[key] = value
        return masked

    def pseudonymize_record(
        self, record: dict[str, Any], salt: str, language: str = "de"
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """
        Pseudonymizes PII in a record.
        Returns (pseudonymized record, mapping original→pseudonym).
        """
        pseudonymized = {}
        mapping: dict[str, str] = {}

        for key, value in record.items():
            if isinstance(value, str) and value.strip():
                scan = self.scan_text(value, language)
                if scan.contains_pii:
                    pseudo_text, text_mapping = self.pseudonymize_text(
                        value, salt, language
                    )
                    pseudonymized[key] = pseudo_text
                    mapping.update(text_mapping)
                else:
                    pseudonymized[key] = value
            else:
                pseudonymized[key] = value

        return pseudonymized, mapping


# ── Convenience ─────────────────────────────────────────────

_default_scanner: PIIScanner | None = None

def get_scanner() -> PIIScanner:
    """Returns a singleton instance of the scanner."""
    global _default_scanner
    if _default_scanner is None:
        config = load_config()
        _default_scanner = PIIScanner(config=config)
    return _default_scanner
