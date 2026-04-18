"""Tests for POST /preview (dry-run Pipeline Inspector endpoint).

The endpoint glues extract + scan + quality + OPA privacy into one
side-effect-free dry-run. These tests mock the scanner and the OPA
HTTP calls so we can assert the contract without running Presidio or
Postgres.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from pii_scanner import PIIScanResult


@pytest.fixture
def client(monkeypatch):
    """TestClient with scanner + OPA calls mocked."""
    with patch("ingestion_api.get_scanner") as mock_get_scanner:
        scanner = MagicMock()
        mock_get_scanner.return_value = scanner
        # Default: no PII, empty scan.
        scanner.scan_text.return_value = PIIScanResult()

        # Default OPA responses — overridden per-test when needed.
        async def _default_quality(source_type, score):
            return {"allowed": True, "min_score": 0.3, "reason": ""}
        async def _default_privacy(classification, contains_pii, legal_basis):
            return {
                "pii_action":           "mask",
                "dual_storage_enabled": False,
                "retention_days":       365,
            }
        monkeypatch.setattr("ingestion_api.check_opa_quality_gate", _default_quality)
        monkeypatch.setattr("ingestion_api.check_opa_privacy", _default_privacy)

        from ingestion_api import app

        with TestClient(app, raise_server_exceptions=False) as tc:
            tc._mock_scanner = scanner
            yield tc


class TestPreviewValidation:
    def test_rejects_missing_input(self, client):
        resp = client.post("/preview", json={})
        assert resp.status_code == 400
        assert "text" in resp.json()["detail"].lower()

    def test_accepts_text_input(self, client):
        resp = client.post("/preview", json={"text": "Hallo Welt"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["extract"]["status"] == "skipped"
        assert "duration_ms" in body["scan"]
        assert "gate_allowed" in body["quality"]


class TestPreviewNoPII:
    def test_clean_text_would_ingest(self, client):
        resp = client.post("/preview", json={
            "text": "Dies ist ein Test-Dokument ohne personenbezogene Daten. "
                    "Es beschreibt die API-Architektur und enthält keine Namen.",
            "classification": "internal",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["scan"]["contains_pii"] is False
        assert body["privacy"]["pii_action"] == "mask"
        assert body["summary"]["would_ingest"] is True
        # No PII → no entity badges.
        assert body["scan"]["entity_counts"] == {}


class TestPreviewWithPII:
    def test_confidential_without_legal_basis_blocks(self, client, monkeypatch):
        """Mirrors the real OPA rule: confidential + PII + no legal_basis → block."""
        client._mock_scanner.scan_text.return_value = PIIScanResult(
            contains_pii=True,
            entity_counts={"PERSON": 1, "EMAIL_ADDRESS": 1},
            entity_locations=[
                {"type": "PERSON", "start": 0, "end": 10,
                 "score": 0.95, "text_snippet": "Anna Mueller"},
            ],
        )

        async def _privacy_block(classification, contains_pii, legal_basis):
            assert contains_pii is True
            assert legal_basis in (None, "")
            return {
                "pii_action":           "block",
                "dual_storage_enabled": True,
                "retention_days":       365,
            }
        monkeypatch.setattr("ingestion_api.check_opa_privacy", _privacy_block)

        resp = client.post("/preview", json={
            "text": "Anna Mueller lebt in Berlin.",
            "classification": "confidential",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["privacy"]["pii_action"] == "block"
        assert body["summary"]["would_ingest"] is False
        assert any("pii_action=block" in r for r in body["summary"]["reasons"])

    def test_confidential_with_legal_basis_vaults(self, client, monkeypatch):
        client._mock_scanner.scan_text.return_value = PIIScanResult(
            contains_pii=True,
            entity_counts={"PERSON": 1},
            entity_locations=[],
        )

        async def _priv_vault(classification, contains_pii, legal_basis):
            assert legal_basis == "contract_fulfillment"
            return {
                "pii_action":           "encrypt_and_store",
                "dual_storage_enabled": True,
                "retention_days":       730,
            }
        monkeypatch.setattr("ingestion_api.check_opa_privacy", _priv_vault)

        resp = client.post("/preview", json={
            "text": "Vertragspartner Anna Mueller.",
            "classification": "confidential",
            "legal_basis": "contract_fulfillment",
        })
        body = resp.json()
        assert body["privacy"]["pii_action"] == "encrypt_and_store"
        assert body["privacy"]["dual_storage_enabled"] is True
        assert body["privacy"]["retention_days"] == 730
        assert body["summary"]["would_ingest"] is True


class TestPreviewQualityGate:
    def test_low_quality_text_not_ingested(self, client, monkeypatch):
        async def _deny_gate(source_type, score):
            return {"allowed": False, "min_score": 0.6,
                    "reason": f"score {score:.2f} below min 0.60"}
        monkeypatch.setattr("ingestion_api.check_opa_quality_gate", _deny_gate)

        resp = client.post("/preview", json={
            "text": "hi",  # trivially short
            "source_type": "default",
        })
        body = resp.json()
        assert body["quality"]["gate_allowed"] is False
        assert body["summary"]["would_ingest"] is False
        assert any("quality gate" in r for r in body["summary"]["reasons"])


class TestPreviewResponseShape:
    def test_has_all_five_phases(self, client):
        resp = client.post("/preview", json={
            "text": "Simple text for shape check."
        })
        body = resp.json()
        assert set(body) == {"extract", "scan", "quality", "privacy", "summary"}
        # Durations populated even for the skipped-extract case.
        assert "duration_ms" in body["scan"]
        assert "duration_ms" in body["quality"]
        assert "duration_ms" in body["privacy"]
        assert "duration_ms" in body["summary"]

    def test_entity_locations_capped(self, client):
        """UI shouldn't render hundreds of rows — the endpoint caps at 20."""
        many = [
            {"type": "PERSON", "start": i, "end": i + 3,
             "score": 0.9, "text_snippet": "x"}
            for i in range(40)
        ]
        client._mock_scanner.scan_text.return_value = PIIScanResult(
            contains_pii=True,
            entity_counts={"PERSON": 40},
            entity_locations=many,
        )
        resp = client.post("/preview", json={"text": "irrelevant"})
        body = resp.json()
        assert len(body["scan"]["entity_locations"]) == 20
