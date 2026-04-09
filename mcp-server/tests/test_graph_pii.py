"""Tests for B-30: PII masking in graph query results."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import server


@pytest.fixture
def mock_scan_pii(mock_http_client):
    """Mock the PII scanner to detect PII."""
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "contains_pii": True,
        "masked_text": "<PERSON>\n---PII_DELIM---\n<PERSON>\n---PII_DELIM---\n<EMAIL_ADDRESS>",
        "entity_types": ["PERSON", "EMAIL_ADDRESS"],
    }
    mock_http_client.post.return_value = response
    with patch.object(server, "http", mock_http_client):
        yield mock_http_client


@pytest.fixture
def mock_scan_no_pii(mock_http_client):
    """Mock the PII scanner to find no PII."""
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "contains_pii": False,
        "masked_text": "Max\n---PII_DELIM---\nMustermann\n---PII_DELIM---\nmax@example.com",
        "entity_types": [],
    }
    mock_http_client.post.return_value = response
    with patch.object(server, "http", mock_http_client):
        yield mock_http_client


@pytest.fixture
def mock_scan_failure(mock_http_client):
    """Mock the PII scanner to fail."""
    mock_http_client.post.side_effect = Exception("Connection refused")
    with patch.object(server, "http", mock_http_client):
        yield mock_http_client


class TestMaskGraphPii:
    """Tests for _mask_graph_pii helper."""

    @pytest.mark.asyncio
    async def test_masks_pii_in_person_node(self, mock_scan_pii):
        data = {
            "nodes": [
                {
                    "id": "person-1",
                    "label": "Person",
                    "firstname": "Max",
                    "lastname": "Mustermann",
                    "email": "max@example.com",
                    "role": "developer",
                }
            ],
            "count": 1,
        }
        result = await server._mask_graph_pii(data)

        # PII fields should be masked
        node = result["nodes"][0]
        assert node["firstname"] == "<PERSON>"
        assert node["lastname"] == "<PERSON>"
        assert node["email"] == "<EMAIL_ADDRESS>"
        # Non-PII fields unchanged
        assert node["role"] == "developer"
        assert node["id"] == "person-1"

    @pytest.mark.asyncio
    async def test_no_pii_returns_unchanged(self, mock_scan_no_pii):
        data = {
            "nodes": [
                {"id": "proj-1", "label": "Project", "description": "A project"},
            ],
            "count": 1,
        }
        result = await server._mask_graph_pii(data)
        assert result["nodes"][0]["description"] == "A project"

    @pytest.mark.asyncio
    async def test_no_pii_keys_skips_scan(self, mock_http_client):
        """When no PII-sensitive keys exist, scanner should not be called."""
        with patch.object(server, "http", mock_http_client):
            data = {"nodes": [{"id": "1", "label": "Project", "status": "active"}], "count": 1}
            result = await server._mask_graph_pii(data)
            mock_http_client.post.assert_not_called()
            assert result["nodes"][0]["status"] == "active"

    @pytest.mark.asyncio
    async def test_scanner_failure_returns_unmasked(self, mock_scan_failure):
        data = {
            "nodes": [{"firstname": "Max", "lastname": "Mustermann"}],
            "count": 1,
        }
        result = await server._mask_graph_pii(data)
        # Data should be returned unchanged on failure
        assert result["nodes"][0]["firstname"] == "Max"
        assert result["nodes"][0]["lastname"] == "Mustermann"

    @pytest.mark.asyncio
    async def test_nested_relationships(self, mock_scan_pii):
        """Relationships have nested dicts (a, r, b columns)."""
        mock_scan_pii.post.return_value.json.return_value = {
            "contains_pii": True,
            "masked_text": "<PERSON>",
            "entity_types": ["PERSON"],
        }
        data = {
            "relationships": [
                {
                    "a": {"id": "p1", "label": "Person", "name": "Alice"},
                    "r": {"type": "OWNS"},
                    "b": {"id": "proj1", "label": "Project", "title": "Foo"},
                }
            ],
            "count": 1,
        }
        result = await server._mask_graph_pii(data)
        # "name" is a PII key → should be masked in the nested dict
        assert result["relationships"][0]["a"]["name"] == "<PERSON>"
        # Non-PII nested dicts unchanged
        assert result["relationships"][0]["b"]["title"] == "Foo"

    @pytest.mark.asyncio
    async def test_empty_data(self, mock_http_client):
        with patch.object(server, "http", mock_http_client):
            result = await server._mask_graph_pii({})
            assert result == {}

    @pytest.mark.asyncio
    async def test_list_input(self, mock_http_client):
        with patch.object(server, "http", mock_http_client):
            result = await server._mask_graph_pii([])
            assert result == []

    @pytest.mark.asyncio
    async def test_scalar_passthrough(self, mock_http_client):
        with patch.object(server, "http", mock_http_client):
            assert await server._mask_graph_pii("hello") == "hello"
            assert await server._mask_graph_pii(42) == 42
