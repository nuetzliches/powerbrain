"""Verify PII scanner configures Presidio with German + English NLP models."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER_FILE = ROOT / "ingestion" / "pii_scanner.py"


class TestPIIScannerConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCANNER_FILE.read_text(encoding="utf-8")

    def test_imports_nlp_engine_provider(self):
        """NlpEngineProvider must be imported to configure spaCy models."""
        self.assertIn("NlpEngineProvider", self.source,
                       "Must import NlpEngineProvider from presidio_analyzer.nlp_engine")

    def test_configures_german_model(self):
        """de_core_news_md must be configured for German language support."""
        self.assertIn("de_core_news_md", self.source,
                       "Must configure de_core_news_md spaCy model for German")

    def test_configures_english_model(self):
        """en_core_web_lg must be configured for English language support."""
        self.assertIn("en_core_web_lg", self.source,
                       "Must configure en_core_web_lg spaCy model for English")

    def test_nlp_engine_passed_to_analyzer(self):
        """AnalyzerEngine must receive the configured NLP engine."""
        # Find the __init__ method
        init_start = self.source.index("def __init__")
        init_section = self.source[init_start:init_start + 1000]
        self.assertIn("nlp_engine=", init_section,
                       "AnalyzerEngine must receive nlp_engine parameter")

    def test_supported_languages_configured(self):
        """AnalyzerEngine must be told which languages are supported."""
        init_start = self.source.index("def __init__")
        init_section = self.source[init_start:init_start + 1000]
        self.assertIn("supported_languages=", init_section,
                       "AnalyzerEngine must receive supported_languages parameter")

    def test_nlp_config_has_both_languages(self):
        """NLP config must specify models for both 'de' and 'en'."""
        init_start = self.source.index("def __init__")
        init_section = self.source[init_start:init_start + 1000]
        self.assertIn('"de"', init_section,
                       "NLP config must include German language code")
        self.assertIn('"en"', init_section,
                       "NLP config must include English language code")


if __name__ == "__main__":
    unittest.main()
