"""Tests for PIIScanner with mocked Presidio."""

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from pii_scanner import (
    PIIScanner,
    PIIScanResult,
    PIIScannerConfig,
    PatternConfig,
    RecognizerConfig,
    LanguageConfig,
    load_config,
)


@pytest.fixture
def scanner():
    """Create a PIIScanner with mocked NLP engine."""
    with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
         patch("pii_scanner.AnalyzerEngine") as mock_analyzer_cls, \
         patch("pii_scanner.AnonymizerEngine") as mock_anonymizer_cls:

        mock_engine = MagicMock()
        mock_provider.return_value.create_engine.return_value = mock_engine

        scanner = PIIScanner(languages=["de"])
        yield scanner


class TestScanText:
    def test_no_pii_returns_empty(self, scanner):
        scanner.analyzer.analyze.return_value = []

        result = scanner.scan_text("Hallo Welt")
        assert result.contains_pii is False
        assert result.entity_counts == {}

    def test_detects_pii(self, scanner):
        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        mock_result.start = 0
        mock_result.end = 15
        mock_result.score = 0.95
        scanner.analyzer.analyze.return_value = [mock_result]

        result = scanner.scan_text("Max Mustermann ist hier")
        assert result.contains_pii is True
        assert result.entity_counts["PERSON"] == 1
        assert len(result.entity_locations) == 1

    def test_multiple_entities(self, scanner):
        mock_person = MagicMock()
        mock_person.entity_type = "PERSON"
        mock_person.start = 0
        mock_person.end = 3
        mock_person.score = 0.9

        mock_email = MagicMock()
        mock_email.entity_type = "EMAIL_ADDRESS"
        mock_email.start = 10
        mock_email.end = 25
        mock_email.score = 0.99

        scanner.analyzer.analyze.return_value = [mock_person, mock_email]

        result = scanner.scan_text("Max sendet max@example.com")
        assert result.contains_pii is True
        assert result.entity_counts["PERSON"] == 1
        assert result.entity_counts["EMAIL_ADDRESS"] == 1
        assert len(result.entity_locations) == 2

    def test_empty_text_returns_empty(self, scanner):
        result = scanner.scan_text("")
        assert result.contains_pii is False
        scanner.analyzer.analyze.assert_not_called()

    def test_whitespace_only_returns_empty(self, scanner):
        result = scanner.scan_text("   \n\t  ")
        assert result.contains_pii is False
        scanner.analyzer.analyze.assert_not_called()

    def test_entity_location_includes_snippet(self, scanner):
        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        mock_result.start = 5
        mock_result.end = 8
        mock_result.score = 0.85
        scanner.analyzer.analyze.return_value = [mock_result]

        result = scanner.scan_text("Hallo Max hier")
        loc = result.entity_locations[0]
        assert loc["type"] == "PERSON"
        assert loc["start"] == 5
        assert loc["end"] == 8
        assert loc["score"] == 0.85


class TestMaskText:
    def test_masks_pii(self, scanner):
        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        scanner.analyzer.analyze.return_value = [mock_result]

        mock_anonymized = MagicMock()
        mock_anonymized.text = "<PERSON> ist hier"
        scanner.anonymizer.anonymize.return_value = mock_anonymized

        result = scanner.mask_text("Max Mustermann ist hier")
        assert result == "<PERSON> ist hier"

    def test_mask_calls_anonymizer(self, scanner):
        scanner.analyzer.analyze.return_value = []
        mock_anonymized = MagicMock()
        mock_anonymized.text = "no pii here"
        scanner.anonymizer.anonymize.return_value = mock_anonymized

        scanner.mask_text("no pii here")
        scanner.anonymizer.anonymize.assert_called_once()


class TestPseudonymizeText:
    def test_deterministic_pseudonyms(self, scanner):
        mock_r1 = MagicMock()
        mock_r1.entity_type = "PERSON"
        mock_r1.start = 0
        mock_r1.end = 3
        mock_r1.score = 0.9
        scanner.analyzer.analyze.return_value = [mock_r1]

        text = "Max is here"
        result1, map1 = scanner.pseudonymize_text(text, "salt1")
        result2, map2 = scanner.pseudonymize_text(text, "salt1")

        assert map1 == map2
        assert result1 == result2

    def test_different_salt_different_pseudonym(self, scanner):
        mock_r = MagicMock()
        mock_r.entity_type = "PERSON"
        mock_r.start = 0
        mock_r.end = 3
        mock_r.score = 0.9
        scanner.analyzer.analyze.return_value = [mock_r]

        _, map1 = scanner.pseudonymize_text("Max is here", "salt1")
        _, map2 = scanner.pseudonymize_text("Max is here", "salt2")

        assert map1["Max"] != map2["Max"]
        # Both should still have typed format
        assert map1["Max"].startswith("[PERSON:")
        assert map2["Max"].startswith("[PERSON:")

    def test_no_pii_returns_original(self, scanner):
        scanner.analyzer.analyze.return_value = []
        result, mapping = scanner.pseudonymize_text("No PII here", "salt")
        assert result == "No PII here"
        assert mapping == {}

    def test_pseudonym_replaces_text(self, scanner):
        mock_r = MagicMock()
        mock_r.entity_type = "PERSON"
        mock_r.start = 0
        mock_r.end = 3
        mock_r.score = 0.9
        scanner.analyzer.analyze.return_value = [mock_r]

        result, mapping = scanner.pseudonymize_text("Max is here", "salt")
        assert "Max" not in result
        assert mapping["Max"] in result

    def test_typed_format_in_mapping(self, scanner):
        """Pseudonyms in mapping must use [TYPE:hash] format."""
        mock_r = MagicMock()
        mock_r.entity_type = "PERSON"
        mock_r.start = 0
        mock_r.end = 3
        mock_r.score = 0.9
        scanner.analyzer.analyze.return_value = [mock_r]

        _, mapping = scanner.pseudonymize_text("Max is here", "salt")
        pseudo = mapping["Max"]
        assert pseudo.startswith("[PERSON:")
        assert pseudo.endswith("]")
        # 8-char hex between colon and bracket
        hex_part = pseudo[len("[PERSON:"):-1]
        assert len(hex_part) == 8
        int(hex_part, 16)  # must be valid hex

    def test_typed_format_in_text(self, scanner):
        """Verify [EMAIL_ADDRESS:hash] format appears in pseudonymized text."""
        mock_r = MagicMock()
        mock_r.entity_type = "EMAIL_ADDRESS"
        mock_r.start = 9
        mock_r.end = 24
        mock_r.score = 0.99
        scanner.analyzer.analyze.return_value = [mock_r]

        text = "Kontakt: max@example.com bitte"
        result, mapping = scanner.pseudonymize_text(text, "project-salt")
        pseudo = mapping["max@example.com"]
        assert pseudo.startswith("[EMAIL_ADDRESS:")
        assert pseudo.endswith("]")
        assert "[EMAIL_ADDRESS:" in result
        assert "max@example.com" not in result


class TestPIIScannerConfig:
    """Tests for Pydantic config models, YAML loading, and config-driven scanner."""

    # ── Config model defaults ───────────────────────────────

    def test_default_config(self):
        cfg = PIIScannerConfig()
        assert cfg.min_confidence == 0.7
        assert len(cfg.languages) == 2
        assert cfg.languages[0].code == "de"
        assert cfg.languages[1].code == "en"
        assert "PERSON" in cfg.entity_types
        assert cfg.custom_recognizers == []

    def test_all_entity_types_without_custom(self):
        cfg = PIIScannerConfig(entity_types=["PERSON", "EMAIL_ADDRESS"])
        assert cfg.all_entity_types == ["PERSON", "EMAIL_ADDRESS"]

    def test_all_entity_types_with_custom(self):
        cfg = PIIScannerConfig(
            entity_types=["PERSON"],
            custom_recognizers=[
                RecognizerConfig(
                    name="Tax",
                    entity_type="DE_TAX_ID",
                    patterns=[PatternConfig(name="tax", regex=r"\d+")],
                ),
            ],
        )
        assert cfg.all_entity_types == ["PERSON", "DE_TAX_ID"]

    def test_all_entity_types_deduplicates(self):
        """If a custom recognizer's entity_type is already in entity_types, no duplicate."""
        cfg = PIIScannerConfig(
            entity_types=["PERSON", "DE_TAX_ID"],
            custom_recognizers=[
                RecognizerConfig(
                    name="Tax",
                    entity_type="DE_TAX_ID",
                    patterns=[PatternConfig(name="tax", regex=r"\d+")],
                ),
            ],
        )
        assert cfg.all_entity_types.count("DE_TAX_ID") == 1
        assert cfg.all_entity_types == ["PERSON", "DE_TAX_ID"]

    def test_pattern_config_defaults(self):
        p = PatternConfig(name="test", regex=r"\d+")
        assert p.score == 0.6

    def test_recognizer_config_defaults(self):
        r = RecognizerConfig(
            name="Test",
            entity_type="TEST",
            patterns=[PatternConfig(name="p", regex=r"\d+")],
        )
        assert r.language == "de"

    # ── YAML loading ────────────────────────────────────────

    def test_load_config_from_file(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            min_confidence: 0.8
            languages:
              - code: de
                model: de_core_news_md
            entity_types:
              - PERSON
              - EMAIL_ADDRESS
            custom_recognizers:
              - name: Test Recognizer
                entity_type: TEST_ENTITY
                language: de
                patterns:
                  - name: test_pattern
                    regex: '\\\\b\\\\d{4}\\\\b'
                    score: 0.5
        """)
        config_file = tmp_path / "test_config.yaml"
        config_file.write_text(yaml_content)

        cfg = load_config(config_file)
        assert cfg.min_confidence == 0.8
        assert len(cfg.languages) == 1
        assert cfg.languages[0].code == "de"
        assert cfg.entity_types == ["PERSON", "EMAIL_ADDRESS"]
        assert len(cfg.custom_recognizers) == 1
        assert cfg.custom_recognizers[0].entity_type == "TEST_ENTITY"

    def test_load_config_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg == PIIScannerConfig()

    def test_load_config_env_var(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            min_confidence: 0.9
            languages:
              - code: en
                model: en_core_web_lg
            entity_types:
              - PERSON
        """)
        config_file = tmp_path / "env_config.yaml"
        config_file.write_text(yaml_content)

        with patch.dict(os.environ, {"PII_CONFIG_PATH": str(config_file)}):
            cfg = load_config()
        assert cfg.min_confidence == 0.9
        assert cfg.languages[0].code == "en"

    def test_load_config_from_real_yaml(self):
        """Load the actual pii_config.yaml shipped with the project."""
        config_path = Path(__file__).parent.parent / "pii_config.yaml"
        if not config_path.exists():
            pytest.skip("pii_config.yaml not found")
        cfg = load_config(config_path)
        assert cfg.min_confidence == 0.7
        assert "PERSON" in cfg.entity_types
        assert len(cfg.custom_recognizers) == 2
        assert cfg.custom_recognizers[0].entity_type == "DE_TAX_ID"
        assert cfg.custom_recognizers[1].entity_type == "DE_SOCIAL_SECURITY"
        assert "DE_TAX_ID" in cfg.all_entity_types
        assert "DE_SOCIAL_SECURITY" in cfg.all_entity_types

    # ── Scanner init with config ────────────────────────────

    def test_scanner_init_with_config(self):
        """PIIScanner accepts a config kwarg and uses it."""
        cfg = PIIScannerConfig(
            min_confidence=0.5,
            languages=[LanguageConfig(code="de", model="de_core_news_md")],
            entity_types=["PERSON"],
            custom_recognizers=[
                RecognizerConfig(
                    name="Tax",
                    entity_type="DE_TAX_ID",
                    patterns=[PatternConfig(name="tax", regex=r"\d+", score=0.6)],
                ),
            ],
        )

        with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
             patch("pii_scanner.AnalyzerEngine") as mock_analyzer_cls, \
             patch("pii_scanner.AnonymizerEngine"):

            mock_provider.return_value.create_engine.return_value = MagicMock()
            scanner = PIIScanner(config=cfg)

            assert scanner.config is cfg
            assert scanner.config.min_confidence == 0.5
            assert scanner.config.all_entity_types == ["PERSON", "DE_TAX_ID"]
            assert scanner.languages == ["de"]
            # Custom recognizer should be registered
            scanner.analyzer.registry.add_recognizer.assert_called_once()

    def test_scanner_init_legacy_languages(self):
        """Legacy PIIScanner(languages=[...]) still works and builds config."""
        with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
             patch("pii_scanner.AnalyzerEngine"), \
             patch("pii_scanner.AnonymizerEngine"):

            mock_provider.return_value.create_engine.return_value = MagicMock()
            scanner = PIIScanner(languages=["de"])

            assert scanner.config.min_confidence == 0.7
            assert scanner.languages == ["de"]
            assert len(scanner.config.languages) == 1
            assert scanner.config.languages[0].code == "de"
            assert scanner.config.languages[0].model == "de_core_news_md"

    def test_scanner_uses_config_entity_types(self):
        """scan_text passes config entity types to analyzer."""
        cfg = PIIScannerConfig(
            entity_types=["PERSON", "EMAIL_ADDRESS"],
            custom_recognizers=[
                RecognizerConfig(
                    name="Custom",
                    entity_type="CUSTOM_TYPE",
                    patterns=[PatternConfig(name="p", regex=r"\d+")],
                ),
            ],
        )

        with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
             patch("pii_scanner.AnalyzerEngine") as mock_analyzer_cls, \
             patch("pii_scanner.AnonymizerEngine"):

            mock_provider.return_value.create_engine.return_value = MagicMock()
            scanner = PIIScanner(config=cfg)
            scanner.analyzer.analyze.return_value = []

            scanner.scan_text("test text")

            call_kwargs = scanner.analyzer.analyze.call_args
            assert call_kwargs.kwargs["entities"] == ["PERSON", "EMAIL_ADDRESS", "CUSTOM_TYPE"]
            assert call_kwargs.kwargs["score_threshold"] == 0.7

    def test_scanner_uses_config_confidence(self):
        """scan_text uses min_confidence from config."""
        cfg = PIIScannerConfig(min_confidence=0.5, entity_types=["PERSON"])

        with patch("pii_scanner.NlpEngineProvider") as mock_provider, \
             patch("pii_scanner.AnalyzerEngine"), \
             patch("pii_scanner.AnonymizerEngine"):

            mock_provider.return_value.create_engine.return_value = MagicMock()
            scanner = PIIScanner(config=cfg)
            scanner.analyzer.analyze.return_value = []

            scanner.scan_text("test")

            call_kwargs = scanner.analyzer.analyze.call_args
            assert call_kwargs.kwargs["score_threshold"] == 0.5
