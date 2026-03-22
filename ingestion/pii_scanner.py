"""
PII-Scanner für die Ingestion Pipeline
========================================
Erkennt personenbezogene Daten in eingehenden Dokumenten und Datensätzen.
Verwendet Microsoft Presidio (Open Source) für die NER-basierte Erkennung
und ergänzt regex-basierte Muster für DE-spezifische Formate.

Konfiguration über pii_config.yaml (Pfad via PII_CONFIG_PATH env var).

Abhängigkeiten (requirements.txt):
  presidio-analyzer
  presidio-anonymizer
  spacy
  pydantic
  pyyaml
  # Nach Installation: python -m spacy download de_core_news_md
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

log = logging.getLogger("kb-pii")


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


# ── Scan-Ergebnis ──────────────────────────────────────────

@dataclass
class PIIScanResult:
    """Ergebnis eines PII-Scans."""
    contains_pii: bool = False
    entity_counts: dict[str, int] = field(default_factory=dict)
    entity_locations: list[dict[str, Any]] = field(default_factory=list)
    pii_fields: list[str] = field(default_factory=list)
    anonymized_text: str | None = None
    pseudonym_map: dict[str, str] = field(default_factory=dict)


# ── Scanner ─────────────────────────────────────────────────

class PIIScanner:
    """
    Scannt Text und strukturierte Daten auf personenbezogene Informationen.
    
    Verwendung:
        scanner = PIIScanner()
        
        # Freitext scannen
        result = scanner.scan_text("Kontakt: Max Mustermann, max@firma.de")
        
        # Strukturierte Daten (z.B. CSV-Zeile als Dict)
        result = scanner.scan_record({"name": "Max Mustermann", "email": "max@firma.de"})
        
        # Text maskieren
        masked = scanner.mask_text("Rufen Sie 0171-1234567 an")
        # → "Rufen Sie <PHONE_NUMBER> an"
        
        # Text pseudonymisieren (reversibel mit Salt)
        pseudo, mapping = scanner.pseudonymize_text("Max Mustermann bestellt", salt="project-key")
        # → pseudo = "a3f8c1d2 bestellt", mapping = {"Max Mustermann": "a3f8c1d2"}
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
        """Scannt Freitext auf PII."""
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

        entity_counts: dict[str, int] = {}
        entity_locations = []

        for r in results:
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
        """Scannt eine strukturierte Datenzeile (z.B. CSV-Row) Feld für Feld."""
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
        """Ersetzt PII durch Platzhalter: 'Max Mustermann' → '<PERSON>'."""
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )

        anonymized = self.anonymizer.anonymize(
            text=text,
            analyzer_results=results,
        )
        return anonymized.text

    def pseudonymize_text(
        self, text: str, salt: str, language: str = "de"
    ) -> tuple[str, dict[str, str]]:
        """
        Ersetzt PII durch deterministische Pseudonyme.
        Gleicher Input + Salt → gleiches Pseudonym (für Verknüpfbarkeit).

        Returns:
            Tuple aus (pseudonymisierter Text, Mapping {original → pseudonym})
        """
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )

        def make_pseudonym(entity_text: str, entity_type: str) -> str:
            h = hashlib.sha256(f"{salt}:{entity_text}".encode()).hexdigest()[:8]
            return f"[{entity_type}:{h}]"

        # Baue individuelle Pseudonyme pro Ergebnis (nicht pro Entity-Typ),
        # damit mehrere Entities gleichen Typs unterschiedliche Pseudonyme bekommen.
        mapping: dict[str, str] = {}
        for r in results:
            original = text[r.start:r.end]
            pseudo = make_pseudonym(original, r.entity_type)
            mapping[original] = pseudo

        # Manuell ersetzen statt Presidio's anonymizer (der per-Typ-Operatoren braucht).
        # Sortiere nach Position absteigend, damit Offsets stabil bleiben.
        pseudonymized = text
        for r in sorted(results, key=lambda x: x.start, reverse=True):
            original = pseudonymized[r.start:r.end]
            pseudo = mapping.get(original, make_pseudonym(original, r.entity_type))
            pseudonymized = pseudonymized[:r.start] + pseudo + pseudonymized[r.end:]

        return pseudonymized, mapping

    def mask_record(self, record: dict[str, Any], language: str = "de") -> dict[str, Any]:
        """Maskiert PII in allen String-Feldern eines Records."""
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
        Pseudonymisiert PII in einem Record.
        Gibt (pseudonymisierter Record, Mapping original→pseudonym) zurück.
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
    """Gibt eine Singleton-Instanz des Scanners zurück."""
    global _default_scanner
    if _default_scanner is None:
        config = load_config()
        _default_scanner = PIIScanner(config=config)
    return _default_scanner
