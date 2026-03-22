"""Tests for PIIScanner with mocked Presidio."""

from unittest.mock import MagicMock, patch
import pytest

from pii_scanner import PIIScanner, PIIScanResult


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
