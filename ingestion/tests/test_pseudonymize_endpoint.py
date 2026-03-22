"""Tests for POST /pseudonymize endpoint."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from pii_scanner import PIIScanResult


@pytest.fixture
def client():
    """Create a TestClient with mocked scanner."""
    with patch("ingestion_api.get_scanner") as mock_get_scanner:
        mock_scanner = MagicMock()
        mock_get_scanner.return_value = mock_scanner

        # Default: no PII
        mock_scanner.scan_text.return_value = PIIScanResult()
        mock_scanner.pseudonymize_text.return_value = ("pseudonymized", {})

        from ingestion_api import app

        with TestClient(app, raise_server_exceptions=False) as tc:
            tc._mock_scanner = mock_scanner
            yield tc


class TestPseudonymizeNoPII:
    def test_returns_original_text(self, client):
        resp = client.post("/pseudonymize", json={
            "text": "Hallo Welt",
            "salt": "test-salt",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "Hallo Welt"
        assert data["mapping"] == {}
        assert data["contains_pii"] is False
        assert data["entity_types"] == []

    def test_scanner_called_with_language(self, client):
        client.post("/pseudonymize", json={
            "text": "Hello world",
            "salt": "s",
            "language": "en",
        })
        client._mock_scanner.scan_text.assert_called_with("Hello world", language="en")

    def test_default_language_is_de(self, client):
        client.post("/pseudonymize", json={
            "text": "Hallo",
            "salt": "s",
        })
        client._mock_scanner.scan_text.assert_called_with("Hallo", language="de")


class TestPseudonymizeWithPII:
    def test_returns_pseudonymized_text_and_mapping(self, client):
        scan_result = PIIScanResult(
            contains_pii=True,
            entity_counts={"PERSON": 1},
        )
        client._mock_scanner.scan_text.return_value = scan_result
        client._mock_scanner.pseudonymize_text.return_value = (
            "abc12345 ist hier",
            {"Max Mustermann": "abc12345"},
        )

        resp = client.post("/pseudonymize", json={
            "text": "Max Mustermann ist hier",
            "salt": "project-salt",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "abc12345 ist hier"
        assert data["mapping"] == {"Max Mustermann": "abc12345"}
        assert data["contains_pii"] is True
        assert data["entity_types"] == ["PERSON"]

    def test_pseudonymize_called_with_salt(self, client):
        scan_result = PIIScanResult(
            contains_pii=True,
            entity_counts={"EMAIL_ADDRESS": 1},
        )
        client._mock_scanner.scan_text.return_value = scan_result
        client._mock_scanner.pseudonymize_text.return_value = (
            "pseudo text",
            {"max@example.com": "pseudo_email"},
        )

        client.post("/pseudonymize", json={
            "text": "max@example.com",
            "salt": "my-salt",
            "language": "en",
        })

        client._mock_scanner.pseudonymize_text.assert_called_once_with(
            "max@example.com", salt="my-salt", language="en",
        )

    def test_multiple_entity_types(self, client):
        scan_result = PIIScanResult(
            contains_pii=True,
            entity_counts={"PERSON": 1, "EMAIL_ADDRESS": 1},
        )
        client._mock_scanner.scan_text.return_value = scan_result
        client._mock_scanner.pseudonymize_text.return_value = (
            "pseudo text",
            {"Max": "p1", "max@example.com": "p2"},
        )

        resp = client.post("/pseudonymize", json={
            "text": "Max max@example.com",
            "salt": "s",
        })

        data = resp.json()
        assert data["contains_pii"] is True
        assert set(data["entity_types"]) == {"PERSON", "EMAIL_ADDRESS"}
        assert len(data["mapping"]) == 2


class TestPseudonymizeValidation:
    def test_missing_text_returns_422(self, client):
        resp = client.post("/pseudonymize", json={
            "salt": "s",
        })
        assert resp.status_code == 422

    def test_missing_salt_returns_422(self, client):
        resp = client.post("/pseudonymize", json={
            "text": "Hallo Welt",
        })
        assert resp.status_code == 422

    def test_empty_text_returns_422(self, client):
        resp = client.post("/pseudonymize", json={
            "text": "",
            "salt": "s",
        })
        assert resp.status_code == 422

    def test_empty_salt_returns_422(self, client):
        resp = client.post("/pseudonymize", json={
            "text": "Hallo",
            "salt": "",
        })
        assert resp.status_code == 422

    def test_missing_body_returns_422(self, client):
        resp = client.post("/pseudonymize")
        assert resp.status_code == 422
