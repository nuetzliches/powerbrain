# Configurable PII Scanner — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the PII scanner fully configurable via YAML (entity types, custom recognizers, confidence, languages).

**Architecture:** Pydantic config models parse `pii_config.yaml` at startup. `PIIScanner.__init__` accepts a config object. `get_scanner()` loads YAML via `PII_CONFIG_PATH` env var. Shipped defaults match today's hardcoded behavior exactly.

**Tech Stack:** Python 3.12+, Pydantic v2, PyYAML, Presidio, pytest

---

### Task 1: Add PyYAML dependency

**Files:**
- Modify: `ingestion/requirements.txt`

**Step 1: Add PyYAML to requirements**

Add `pyyaml>=6.0` to `ingestion/requirements.txt`.

**Step 2: Verify**

Run: `pip install pyyaml` (already likely installed, just ensure it's pinned)

**Step 3: Commit**

```bash
git add ingestion/requirements.txt
git commit -m "chore: add pyyaml dependency for PII scanner config"
```

---

### Task 2: Create pii_config.yaml with shipped defaults

**Files:**
- Create: `ingestion/pii_config.yaml`

**Step 1: Write the YAML file**

```yaml
# PII Scanner Configuration
# =========================
# Controls which PII entities are detected and how.
# Changes require container restart.

# Minimum confidence score for entity detection (0.0 - 1.0)
min_confidence: 0.7

# NLP languages and spaCy models
languages:
  - code: de
    model: de_core_news_md
  - code: en
    model: en_core_web_lg

# Presidio built-in entity types to detect
# Full list: https://microsoft.github.io/presidio/supported_entities/
entity_types:
  - PERSON
  - EMAIL_ADDRESS
  - PHONE_NUMBER
  - IBAN_CODE
  - CREDIT_CARD
  - IP_ADDRESS
  - LOCATION
  - DATE_OF_BIRTH
  # - ORGANIZATION  # uncomment to detect company/org names

# Custom regex-based recognizers
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

**Step 2: Commit**

```bash
git add ingestion/pii_config.yaml
git commit -m "feat: add PII scanner YAML config with shipped defaults"
```

---

### Task 3: Write failing tests for config loading

**Files:**
- Modify: `ingestion/tests/test_pii_scanner.py`

**Step 1: Write tests for Pydantic config models**

Add a new test class `TestPIIScannerConfig` at the top of the file (after imports):

```python
import yaml
from pathlib import Path
from pii_scanner import (
    PIIScanner, PIIScanResult,
    PIIScannerConfig, PatternConfig, RecognizerConfig, LanguageConfig,
    load_config,
)


class TestPIIScannerConfig:
    def test_load_default_config_file(self):
        """Shipped pii_config.yaml loads without errors."""
        config_path = Path(__file__).parent.parent / "pii_config.yaml"
        config = load_config(config_path)
        assert isinstance(config, PIIScannerConfig)
        assert config.min_confidence == 0.7
        assert len(config.languages) == 2
        assert len(config.entity_types) >= 8
        assert len(config.custom_recognizers) == 2

    def test_entity_types_from_config(self):
        """Config entity_types are used by scanner."""
        config = PIIScannerConfig(
            min_confidence=0.5,
            languages=[LanguageConfig(code="en", model="en_core_web_lg")],
            entity_types=["PERSON", "EMAIL_ADDRESS"],
            custom_recognizers=[],
        )
        assert config.entity_types == ["PERSON", "EMAIL_ADDRESS"]
        assert config.min_confidence == 0.5

    def test_custom_recognizer_config(self):
        """Custom recognizer config validates correctly."""
        rec = RecognizerConfig(
            name="Test Recognizer",
            entity_type="CUSTOM_ENTITY",
            language="de",
            patterns=[PatternConfig(name="test", regex=r"\d{4}", score=0.8)],
        )
        assert rec.entity_type == "CUSTOM_ENTITY"
        assert len(rec.patterns) == 1

    def test_invalid_config_raises(self):
        """Missing required fields raise validation error."""
        with pytest.raises(Exception):
            PIIScannerConfig(
                min_confidence=0.7,
                languages=[],
                entity_types=[],
                # custom_recognizers missing
            )

    def test_config_all_entity_types_includes_custom(self):
        """all_entity_types returns built-in + custom entity types."""
        config = PIIScannerConfig(
            min_confidence=0.7,
            languages=[LanguageConfig(code="de", model="de_core_news_md")],
            entity_types=["PERSON"],
            custom_recognizers=[
                RecognizerConfig(
                    name="Test",
                    entity_type="CUSTOM_X",
                    language="de",
                    patterns=[PatternConfig(name="t", regex=r"\d+", score=0.5)],
                ),
            ],
        )
        all_types = config.all_entity_types
        assert "PERSON" in all_types
        assert "CUSTOM_X" in all_types
```

**Step 2: Run tests to verify they fail**

Run: `cd ingestion && python -m pytest tests/test_pii_scanner.py::TestPIIScannerConfig -v`
Expected: ImportError (PIIScannerConfig etc. don't exist yet)

**Step 3: Commit**

```bash
git add ingestion/tests/test_pii_scanner.py
git commit -m "test: add failing tests for PII scanner config models"
```

---

### Task 4: Implement Pydantic config models and load_config

**Files:**
- Modify: `ingestion/pii_scanner.py`

**Step 1: Add imports**

Add at top of file (after existing imports):

```python
import os
from pathlib import Path
import yaml
from pydantic import BaseModel, field_validator
```

**Step 2: Add Pydantic config models**

Replace the `# ── Konfiguration ───` section (lines 29-40) with:

```python
# ── Konfiguration ───────────────────────────────────────────

class PatternConfig(BaseModel):
    """A single regex pattern for a custom recognizer."""
    name: str
    regex: str
    score: float = 0.6

class RecognizerConfig(BaseModel):
    """A custom pattern-based recognizer."""
    name: str
    entity_type: str
    language: str = "de"
    patterns: list[PatternConfig]

class LanguageConfig(BaseModel):
    """Language and spaCy model pairing."""
    code: str
    model: str

class PIIScannerConfig(BaseModel):
    """Top-level PII scanner configuration."""
    min_confidence: float = 0.7
    languages: list[LanguageConfig]
    entity_types: list[str]
    custom_recognizers: list[RecognizerConfig] = []

    @property
    def all_entity_types(self) -> list[str]:
        """Built-in entity types + custom recognizer entity types."""
        custom = [r.entity_type for r in self.custom_recognizers]
        return self.entity_types + [t for t in custom if t not in self.entity_types]


def load_config(path: str | Path | None = None) -> PIIScannerConfig:
    """Load PII scanner configuration from YAML file."""
    if path is None:
        path = os.environ.get("PII_CONFIG_PATH", "pii_config.yaml")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PII config not found: {path}")
    with open(path) as f:
        raw = yaml.safe_load(f)
    return PIIScannerConfig(**raw)
```

**Step 3: Remove old hardcoded constants**

Delete the old `PII_ENTITY_TYPES` list and `MIN_CONFIDENCE` constant (they are now in config).

**Step 4: Remove old hardcoded German recognizer definitions**

Delete the module-level `de_tax_id_pattern`, `de_social_security_pattern`, `de_tax_id_recognizer`, `de_social_security_recognizer` constants (lines 45-69). These are now created dynamically from config.

**Step 5: Run config tests**

Run: `cd ingestion && python -m pytest tests/test_pii_scanner.py::TestPIIScannerConfig -v`
Expected: All 5 tests PASS

**Step 6: Commit**

```bash
git add ingestion/pii_scanner.py
git commit -m "feat: add Pydantic config models and YAML loader for PII scanner"
```

---

### Task 5: Write failing test for config-driven scanner initialization

**Files:**
- Modify: `ingestion/tests/test_pii_scanner.py`

**Step 1: Add test for scanner accepting config**

Add to `TestPIIScannerConfig`:

```python
    def test_scanner_accepts_config(self):
        """PIIScanner can be initialized with a config object."""
        config = PIIScannerConfig(
            min_confidence=0.5,
            languages=[LanguageConfig(code="en", model="en_core_web_lg")],
            entity_types=["PERSON"],
            custom_recognizers=[],
        )
        with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
             patch("pii_scanner.AnalyzerEngine"), \
             patch("pii_scanner.AnonymizerEngine"):
            mock_provider.return_value.create_engine.return_value = MagicMock()
            scanner = PIIScanner(config=config)
            assert scanner.config.min_confidence == 0.5
            assert scanner.config.entity_types == ["PERSON"]

    def test_scanner_registers_custom_recognizers(self):
        """Custom recognizers from config are registered with analyzer."""
        config = PIIScannerConfig(
            min_confidence=0.7,
            languages=[LanguageConfig(code="de", model="de_core_news_md")],
            entity_types=["PERSON"],
            custom_recognizers=[
                RecognizerConfig(
                    name="Test Rec",
                    entity_type="CUSTOM_ENT",
                    language="de",
                    patterns=[PatternConfig(name="test_pat", regex=r"\d{4}", score=0.8)],
                ),
            ],
        )
        with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
             patch("pii_scanner.AnalyzerEngine") as mock_analyzer_cls, \
             patch("pii_scanner.AnonymizerEngine"):
            mock_provider.return_value.create_engine.return_value = MagicMock()
            scanner = PIIScanner(config=config)
            # Verify custom recognizer was registered
            registry = scanner.analyzer.registry
            registry.add_recognizer.assert_called_once()
            call_args = registry.add_recognizer.call_args[0][0]
            assert call_args.supported_entities == ["CUSTOM_ENT"]
```

**Step 2: Run tests to verify they fail**

Run: `cd ingestion && python -m pytest tests/test_pii_scanner.py::TestPIIScannerConfig::test_scanner_accepts_config tests/test_pii_scanner.py::TestPIIScannerConfig::test_scanner_registers_custom_recognizers -v`
Expected: FAIL (scanner doesn't accept config yet)

**Step 3: Commit**

```bash
git add ingestion/tests/test_pii_scanner.py
git commit -m "test: add failing tests for config-driven scanner init"
```

---

### Task 6: Refactor PIIScanner to use config

**Files:**
- Modify: `ingestion/pii_scanner.py`

**Step 1: Refactor `__init__` to accept config**

Replace the current `__init__` with:

```python
    def __init__(self, config: PIIScannerConfig | None = None,
                 languages: list[str] | None = None):
        if config is not None:
            self.config = config
        else:
            # Legacy: build config from parameters for backward compat
            langs = languages or ["de", "en"]
            # This path is only used in tests with mocked NLP
            self.config = PIIScannerConfig(
                min_confidence=0.7,
                languages=[LanguageConfig(code=c, model="") for c in langs],
                entity_types=[
                    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE",
                    "CREDIT_CARD", "IP_ADDRESS", "LOCATION", "DATE_OF_BIRTH",
                ],
                custom_recognizers=[],
            )

        lang_codes = [l.code for l in self.config.languages]

        # Configure NLP engine
        models = [
            {"lang_code": l.code, "model_name": l.model}
            for l in self.config.languages
            if l.model  # skip empty model names (test/legacy path)
        ]
        if models:
            nlp_config = {"nlp_engine_name": "spacy", "models": models}
            provider = NlpEngineProvider(nlp_configuration=nlp_config)
            nlp_engine = provider.create_engine()
        else:
            nlp_engine = None

        self.analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=lang_codes,
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
```

**Step 2: Update scan_text, mask_text, pseudonymize_text**

Replace all three occurrences of `entities=PII_ENTITY_TYPES` with `entities=self.config.all_entity_types` and `score_threshold=MIN_CONFIDENCE` with `score_threshold=self.config.min_confidence`.

In `scan_text` (line ~141):
```python
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )
```

In `mask_text` (line ~194):
```python
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )
```

In `pseudonymize_text` (line ~217):
```python
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=self.config.all_entity_types,
            score_threshold=self.config.min_confidence,
        )
```

**Step 3: Update `get_scanner()` to load config**

```python
def get_scanner() -> PIIScanner:
    """Returns a singleton PIIScanner loaded from YAML config."""
    global _default_scanner
    if _default_scanner is None:
        config = load_config()
        _default_scanner = PIIScanner(config=config)
    return _default_scanner
```

**Step 4: Run all tests**

Run: `cd ingestion && python -m pytest tests/test_pii_scanner.py -v`
Expected: All tests PASS (both old and new)

**Step 5: Commit**

```bash
git add ingestion/pii_scanner.py
git commit -m "feat: refactor PIIScanner to use YAML-driven config"
```

---

### Task 7: Update existing test fixture for backward compat

**Files:**
- Modify: `ingestion/tests/test_pii_scanner.py`

**Step 1: Verify existing `scanner` fixture still works**

The existing fixture uses `PIIScanner(languages=["de"])` which should still work via the legacy path. Verify all existing tests pass unchanged.

Run: `cd ingestion && python -m pytest tests/test_pii_scanner.py -v`
Expected: All tests PASS

**Step 2: Commit (only if fixture needed changes)**

---

### Task 8: Run full test suite and verify

**Files:**
- All ingestion tests

**Step 1: Run complete ingestion test suite**

Run: `cd ingestion && python -m pytest tests/ -v`
Expected: All tests PASS

**Step 2: Verify YAML loads correctly in isolation**

```bash
cd ingestion && python -c "from pii_scanner import load_config; c = load_config(); print(f'entities={c.entity_types}, recognizers={len(c.custom_recognizers)}, confidence={c.min_confidence}')"
```

Expected output: `entities=['PERSON', 'EMAIL_ADDRESS', ...], recognizers=2, confidence=0.7`

**Step 3: Commit (if any fixes needed)**

---

### Task 9: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Add pii_config.yaml to directory structure**

In the directory structure section, under `ingestion/`, add:
```
│   ├── pii_config.yaml    ← PII scanner config (entity types, custom recognizers)
```

**Step 2: Update Key Concepts > Privacy (GDPR) section**

Add bullet point:
```
- **Configurable PII Scanner** — entity types, custom recognizers, confidence, and languages via `ingestion/pii_config.yaml`
```

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document configurable PII scanner in CLAUDE.md"
```
