# Configurable PII Scanner — Design

**Date:** 2026-03-23
**Status:** Approved

## Goal

Make the PII scanner fully configurable via YAML: entity types, custom pattern recognizers, confidence threshold, and language/model settings. No code changes needed to add ORGANIZATION detection or project-specific patterns.

## Current State

- `ingestion/pii_scanner.py` has 10 hardcoded entity types in `PII_ENTITY_TYPES`
- Two custom German recognizers (DE_TAX_ID, DE_SOCIAL_SECURITY) defined as module-level constants
- `MIN_CONFIDENCE = 0.7` hardcoded
- Languages `["de", "en"]` and spaCy models hardcoded in `__init__`
- `get_scanner()` singleton with no configuration input

## Design

### YAML Configuration File

New file `ingestion/pii_config.yaml` shipped with defaults matching today's behavior:

```yaml
min_confidence: 0.7

languages:
  - code: de
    model: de_core_news_md
  - code: en
    model: en_core_web_lg

entity_types:
  - PERSON
  - EMAIL_ADDRESS
  - PHONE_NUMBER
  - IBAN_CODE
  - CREDIT_CARD
  - IP_ADDRESS
  - LOCATION
  - DATE_OF_BIRTH
  # - ORGANIZATION  # uncomment to enable

custom_recognizers:
  - name: German Tax ID
    entity_type: DE_TAX_ID
    language: de
    patterns:
      - name: de_tax_id
        regex: '\b\d{2}\s?\d{3}\s?\d{5}\b'
        score: 0.6

  - name: German Social Security Number
    entity_type: DE_SOCIAL_SECURITY
    language: de
    patterns:
      - name: de_social_security
        regex: '\b\d{2}\s?\d{6}\s?[A-Z]\s?\d{3}\b'
        score: 0.6
```

### Pydantic Config Models

In `pii_scanner.py`, add Pydantic models for config validation:

- `PatternConfig(name, regex, score)` — single regex pattern
- `RecognizerConfig(name, entity_type, language, patterns)` — custom recognizer
- `LanguageConfig(code, model)` — language + spaCy model
- `PIIScannerConfig(min_confidence, languages, entity_types, custom_recognizers)` — top-level

Config is loaded once at startup. Invalid regex or missing fields fail fast with clear error messages.

### Scanner Changes

- `PIIScanner.__init__` accepts optional `PIIScannerConfig`
- Entity type list comes from `config.entity_types + [r.entity_type for r in config.custom_recognizers]`
- Custom recognizers are built from config and registered dynamically
- `get_scanner()` loads YAML via `PII_CONFIG_PATH` env var (default: `pii_config.yaml`)

### What Does NOT Change

- `ingestion_api.py` — still calls `get_scanner()`, no changes
- MCP server integration — unchanged
- OPA policies — unchanged
- `PIIScanResult` dataclass — unchanged
- Scan/mask/pseudonymize logic — identical

### Config Path

`PII_CONFIG_PATH` env var, default `pii_config.yaml` (relative to working dir). Follows existing project pattern.

## Files Affected

| File | Change |
|---|---|
| `ingestion/pii_config.yaml` | New: shipped defaults |
| `ingestion/pii_scanner.py` | Config models, YAML loading, dynamic recognizer registration |
| `ingestion/tests/test_pii_scanner.py` | Tests for config loading, custom recognizers, entity filtering |
