# tests/test_pseudonymize_fix.py
"""Tests that pseudonymize_text handles multiple entities of the same type correctly."""
import unittest
import sys
import pathlib

# Add ingestion dir to path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "ingestion"))


def _make_scanner():
    """Create a PIIScanner with proper NLP config for German."""
    try:
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "de", "model_name": "de_core_news_md"}],
        })
        nlp_engine = provider.create_engine()

        from pii_scanner import PIIScanner, de_tax_id_recognizer, de_social_security_recognizer
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine

        scanner = PIIScanner.__new__(PIIScanner)
        scanner.languages = ["de"]
        scanner.analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["de"])
        scanner.anonymizer = AnonymizerEngine()
        scanner.analyzer.registry.add_recognizer(de_tax_id_recognizer)
        scanner.analyzer.registry.add_recognizer(de_social_security_recognizer)
        return scanner
    except Exception as e:
        print(f"Scanner setup failed: {e}")
        return None


class TestPseudonymizeFix(unittest.TestCase):
    """Test pseudonymize_text produces unique pseudonyms per entity."""

    def setUp(self):
        """Create scanner — requires presidio + spacy model installed."""
        self.scanner = _make_scanner()
        if self.scanner is None:
            self.skipTest("Presidio or spaCy German model not available")

    def test_two_persons_get_different_pseudonyms(self):
        text = "Max Mustermann traf Anna Schmidt im Büro."
        salt = "test-salt"
        result = self.scanner.pseudonymize_text(text, salt)
        # After fix: returns tuple (pseudonymized_text, mapping)
        self.assertIsInstance(result, tuple, "pseudonymize_text should return a tuple")
        pseudo_text, mapping = result
        # The two names should NOT be replaced with the same pseudonym
        import re
        pseudonyms = re.findall(r'\b[0-9a-f]{8}\b', pseudo_text)
        if len(pseudonyms) >= 2:
            self.assertNotEqual(
                pseudonyms[0], pseudonyms[1],
                f"Two different persons got the same pseudonym: {pseudo_text}"
            )

    def test_pseudonymize_text_returns_mapping(self):
        """After fix, pseudonymize_text should return (text, mapping) tuple."""
        text = "Max Mustermann hat die Email max@example.com"
        salt = "test-salt"
        result = self.scanner.pseudonymize_text(text, salt)
        # After fix: returns tuple (pseudonymized_text, mapping_dict)
        self.assertIsInstance(result, tuple, "pseudonymize_text should return (text, mapping) tuple")
        pseudo_text, mapping = result
        self.assertIsInstance(pseudo_text, str)
        self.assertIsInstance(mapping, dict)
        self.assertGreater(len(mapping), 0, "Mapping should contain at least one entry")

    def test_pseudonymize_record_returns_mapping(self):
        """pseudonymize_record should return (record, mapping) tuple."""
        record = {"name": "Max Mustermann", "note": "keine PII hier"}
        salt = "test-salt"
        result = self.scanner.pseudonymize_record(record, salt)
        self.assertIsInstance(result, tuple)
        pseudo_record, mapping = result
        self.assertIsInstance(pseudo_record, dict)
        self.assertIsInstance(mapping, dict)
        # The non-PII field should be unchanged
        self.assertEqual(pseudo_record["note"], "keine PII hier")

    def test_deterministic_pseudonyms(self):
        """Same input + salt should produce same pseudonym."""
        text = "Max Mustermann arbeitet hier."
        salt = "test-salt"
        result1 = self.scanner.pseudonymize_text(text, salt)
        result2 = self.scanner.pseudonymize_text(text, salt)
        self.assertIsInstance(result1, tuple)
        self.assertIsInstance(result2, tuple)
        self.assertEqual(result1[0], result2[0], "Same input+salt should give same output")
        self.assertEqual(result1[1], result2[1], "Same input+salt should give same mapping")


if __name__ == "__main__":
    unittest.main()
