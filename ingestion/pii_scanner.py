"""
PII-Scanner für die Ingestion Pipeline
========================================
Erkennt personenbezogene Daten in eingehenden Dokumenten und Datensätzen.
Verwendet Microsoft Presidio (Open Source) für die NER-basierte Erkennung
und ergänzt regex-basierte Muster für DE-spezifische Formate.

Abhängigkeiten (requirements.txt):
  presidio-analyzer
  presidio-anonymizer
  spacy
  # Nach Installation: python -m spacy download de_core_news_md
"""

import re
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from presidio_anonymizer import AnonymizerEngine
# OperatorConfig no longer needed — pseudonymize_text does manual replacement

log = logging.getLogger("kb-pii")


# ── Konfiguration ───────────────────────────────────────────

# PII-Typen, nach denen gescannt wird
PII_ENTITY_TYPES = [
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE",
    "CREDIT_CARD", "IP_ADDRESS", "LOCATION", "DATE_OF_BIRTH",
    # Eigene deutsche Muster (unten definiert):
    "DE_TAX_ID", "DE_SOCIAL_SECURITY",
]

# Minimaler Konfidenzwert für Erkennung
MIN_CONFIDENCE = 0.7


# ── Deutsche PII-Muster ────────────────────────────────────

de_tax_id_pattern = Pattern(
    name="de_tax_id",
    regex=r"\b\d{2}\s?\d{3}\s?\d{5}\b",
    score=0.6,
)

de_social_security_pattern = Pattern(
    name="de_social_security",
    regex=r"\b\d{2}\s?\d{6}\s?[A-Z]\s?\d{3}\b",
    score=0.6,
)

de_tax_id_recognizer = PatternRecognizer(
    supported_entity="DE_TAX_ID",
    name="German Tax ID Recognizer",
    patterns=[de_tax_id_pattern],
    supported_language="de",
)

de_social_security_recognizer = PatternRecognizer(
    supported_entity="DE_SOCIAL_SECURITY",
    name="German Social Security Number Recognizer",
    patterns=[de_social_security_pattern],
    supported_language="de",
)


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

    def __init__(self, languages: list[str] | None = None):
        self.languages = languages or ["de", "en"]
        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()

        # Deutsche Muster registrieren
        self.analyzer.registry.add_recognizer(de_tax_id_recognizer)
        self.analyzer.registry.add_recognizer(de_social_security_recognizer)

    def scan_text(self, text: str, language: str = "de") -> PIIScanResult:
        """Scannt Freitext auf PII."""
        if not text or not text.strip():
            return PIIScanResult()

        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=PII_ENTITY_TYPES,
            score_threshold=MIN_CONFIDENCE,
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
            entities=PII_ENTITY_TYPES,
            score_threshold=MIN_CONFIDENCE,
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
            entities=PII_ENTITY_TYPES,
            score_threshold=MIN_CONFIDENCE,
        )

        def make_pseudonym(entity_text: str) -> str:
            h = hashlib.sha256(f"{salt}:{entity_text}".encode()).hexdigest()[:8]
            return h

        # Baue individuelle Pseudonyme pro Ergebnis (nicht pro Entity-Typ),
        # damit mehrere Entities gleichen Typs unterschiedliche Pseudonyme bekommen.
        mapping: dict[str, str] = {}
        for r in results:
            original = text[r.start:r.end]
            pseudo = make_pseudonym(original)
            mapping[original] = pseudo

        # Manuell ersetzen statt Presidio's anonymizer (der per-Typ-Operatoren braucht).
        # Sortiere nach Position absteigend, damit Offsets stabil bleiben.
        pseudonymized = text
        for r in sorted(results, key=lambda x: x.start, reverse=True):
            original = pseudonymized[r.start:r.end]
            pseudo = mapping.get(original, make_pseudonym(original))
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
        _default_scanner = PIIScanner()
    return _default_scanner
