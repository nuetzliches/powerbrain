"""Tests for B-31: Metadata PII redaction in search results."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import server


@pytest.fixture(autouse=True)
def _set_pii_metadata_fields(monkeypatch):
    """Ensure PII_METADATA_FIELDS is populated for tests."""
    monkeypatch.setattr(server, "PII_METADATA_FIELDS", {
        "userName": "person",
        "customerName": "person",
        "authorEmail": "email",
        "contactPhone": "phone",
    })


@pytest.fixture
def mock_opa_redact_default(mock_http_client):
    """Mock OPA to return default fields_to_redact (includes person + email)."""
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "result": ["email", "phone", "iban", "birthdate", "address", "person"],
    }
    mock_http_client.post.return_value = response
    with patch.object(server, "http", mock_http_client):
        # Clear cache to avoid stale entries between tests
        server._fields_to_redact_cache.clear()
        yield mock_http_client


@pytest.fixture
def mock_opa_redact_support(mock_http_client):
    """Mock OPA for 'support' purpose (no person, no email)."""
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "result": ["iban", "birthdate", "address"],
    }
    mock_http_client.post.return_value = response
    with patch.object(server, "http", mock_http_client):
        server._fields_to_redact_cache.clear()
        yield mock_http_client


@pytest.fixture
def mock_opa_failure(mock_http_client):
    """Mock OPA to fail."""
    mock_http_client.post.side_effect = Exception("OPA unreachable")
    with patch.object(server, "http", mock_http_client):
        server._fields_to_redact_cache.clear()
        yield mock_http_client


class TestRedactMetadataPii:
    """Tests for _redact_metadata_pii helper."""

    @pytest.mark.asyncio
    async def test_redacts_matching_fields(self, mock_opa_redact_default):
        metadata = {
            "userName": "Max Mustermann",
            "authorEmail": "max@example.com",
            "source": "wiki",
            "project": "demo",
        }
        result = await server._redact_metadata_pii(metadata, "default")
        assert result["userName"] == "<REDACTED>"
        assert result["authorEmail"] == "<REDACTED>"
        assert result["source"] == "wiki"
        assert result["project"] == "demo"

    @pytest.mark.asyncio
    async def test_support_purpose_keeps_names(self, mock_opa_redact_support):
        metadata = {
            "userName": "Max Mustermann",
            "authorEmail": "max@example.com",
            "contactPhone": "+49 123 456",
        }
        result = await server._redact_metadata_pii(metadata, "support")
        # Support purpose does not redact person or email
        assert result["userName"] == "Max Mustermann"
        assert result["authorEmail"] == "max@example.com"
        # But phone is not in support redaction list either (only iban, birthdate, address)
        assert result["contactPhone"] == "+49 123 456"

    @pytest.mark.asyncio
    async def test_opa_failure_redacts_all_pii_fields(self, mock_opa_failure):
        metadata = {
            "userName": "Max Mustermann",
            "authorEmail": "max@example.com",
            "source": "wiki",
        }
        result = await server._redact_metadata_pii(metadata, "default")
        # Fail-closed: all PII metadata fields redacted
        assert result["userName"] == "<REDACTED>"
        assert result["authorEmail"] == "<REDACTED>"
        assert result["source"] == "wiki"

    @pytest.mark.asyncio
    async def test_empty_metadata(self, mock_opa_redact_default):
        result = await server._redact_metadata_pii({}, "default")
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_pii_fields_configured(self, monkeypatch, mock_http_client):
        """When PII_METADATA_FIELDS is empty, metadata passes through."""
        monkeypatch.setattr(server, "PII_METADATA_FIELDS", {})
        with patch.object(server, "http", mock_http_client):
            metadata = {"userName": "Max", "source": "wiki"}
            result = await server._redact_metadata_pii(metadata, "default")
            assert result["userName"] == "Max"

    @pytest.mark.asyncio
    async def test_does_not_mutate_original(self, mock_opa_redact_default):
        metadata = {"userName": "Max", "source": "wiki"}
        result = await server._redact_metadata_pii(metadata, "default")
        # Original should be unchanged
        assert metadata["userName"] == "Max"
        assert result["userName"] == "<REDACTED>"

    @pytest.mark.asyncio
    async def test_empty_string_not_redacted(self, mock_opa_redact_default):
        """Empty string values should not be redacted (no point)."""
        metadata = {"userName": "", "source": "wiki"}
        result = await server._redact_metadata_pii(metadata, "default")
        assert result["userName"] == ""


class TestGetFieldsToRedact:
    """Tests for _get_fields_to_redact helper."""

    @pytest.mark.asyncio
    async def test_caches_opa_result(self, mock_opa_redact_default):
        fields1 = await server._get_fields_to_redact("default")
        fields2 = await server._get_fields_to_redact("default")
        # Should only call OPA once due to caching
        assert mock_opa_redact_default.post.call_count == 1
        assert "person" in fields1
        assert fields1 == fields2

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self, mock_opa_failure):
        fields = await server._get_fields_to_redact("default")
        # Fallback includes all common PII fields
        assert "email" in fields
        assert "person" in fields
        assert "phone" in fields
