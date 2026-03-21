"""Tests for HMAC token validation and PII field redaction."""

import json
import hmac
import hashlib
from datetime import datetime, timezone, timedelta

import server
from server import validate_pii_access_token, redact_fields

TEST_SECRET = "test-secret-key"


def _make_token(payload: dict, secret: str = TEST_SECRET) -> dict:
    """Helper: create a valid signed token."""
    signature = hmac.new(
        secret.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {**payload, "signature": signature}


class TestValidatePiiAccessToken:
    def setup_method(self):
        self._orig_secret = server.VAULT_HMAC_SECRET
        server.VAULT_HMAC_SECRET = TEST_SECRET

    def teardown_method(self):
        server.VAULT_HMAC_SECRET = self._orig_secret

    def test_valid_token(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        result = validate_pii_access_token(token)
        assert result["valid"] is True
        assert result["reason"] == "ok"

    def test_invalid_signature(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        token["signature"] = "deadbeef" * 8
        result = validate_pii_access_token(token)
        assert result["valid"] is False
        assert "signature" in result["reason"].lower()

    def test_expired_token(self):
        expires = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        result = validate_pii_access_token(token)
        assert result["valid"] is False
        assert "expired" in result["reason"].lower()

    def test_missing_expires_at(self):
        token = _make_token({"purpose": "audit"})
        result = validate_pii_access_token(token)
        assert result["valid"] is False

    def test_invalid_expires_at_format(self):
        token = _make_token({"purpose": "audit", "expires_at": "not-a-date"})
        result = validate_pii_access_token(token)
        assert result["valid"] is False
        assert "format" in result["reason"].lower()

    def test_payload_excludes_signature(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires})
        result = validate_pii_access_token(token)
        assert "signature" not in result["payload"]

    def test_wrong_secret_fails(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        token = _make_token({"purpose": "audit", "expires_at": expires}, secret="wrong-key")
        result = validate_pii_access_token(token)
        assert result["valid"] is False


class TestRedactFields:
    def test_redact_email(self):
        text = "Contact max@example.com for info"
        entities = [{"type": "EMAIL_ADDRESS", "start": 8, "end": 23}]
        result = redact_fields(text, entities, {"email"})
        assert "<EMAIL_ADDRESS>" in result
        assert "max@example.com" not in result

    def test_redact_multiple_types(self):
        text = "Max lives in Berlin, call 0151-12345678"
        entities = [
            {"type": "LOCATION", "start": 13, "end": 19},
            {"type": "PHONE_NUMBER", "start": 26, "end": 39},
        ]
        result = redact_fields(text, entities, {"address", "phone"})
        assert "<LOCATION>" in result
        assert "<PHONE_NUMBER>" in result

    def test_no_redaction_for_unmapped_fields(self):
        text = "Some text with data"
        entities = [{"type": "PERSON", "start": 0, "end": 4}]
        result = redact_fields(text, entities, {"unknown_field"})
        assert result == text

    def test_empty_fields_returns_original(self):
        text = "Some text"
        entities = [{"type": "EMAIL_ADDRESS", "start": 0, "end": 4}]
        result = redact_fields(text, entities, set())
        assert result == text

    def test_empty_entities_returns_original(self):
        text = "Some text"
        result = redact_fields(text, pii_entities=[], fields_to_redact={"email"})
        assert result == text

    def test_invalid_offsets_skipped(self):
        text = "Short"
        entities = [{"type": "EMAIL_ADDRESS", "start": 0, "end": 100}]
        result = redact_fields(text, entities, {"email"})
        # bounds check: end > len(text), skipped
        assert result == text
