"""Tests for B-30: PII masking in graph query results.

The implementation was refactored from a per-value Presidio scan (flaky
on non-English names, N HTTP roundtrips per query) to a deterministic
key → entity-type map loaded from OPA. These tests cover both the pure
walker (sync, no network) and the OPA-loader round-trip.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import server


DEMO_KEYS = {
    "name":      "PERSON",
    "fullname":  "PERSON",
    "firstname": "PERSON",
    "lastname":  "PERSON",
    "email":     "EMAIL_ADDRESS",
    "phone":     "PHONE_NUMBER",
}


@pytest.fixture(autouse=True)
def _reset_graph_keys_cache():
    """Prevent cross-test pollution of the module-level OPA cache."""
    server._graph_pii_keys_cache = {}
    server._graph_pii_keys_loaded = False
    yield
    server._graph_pii_keys_cache = {}
    server._graph_pii_keys_loaded = False


# ── Pure walker (no network) ───────────────────────────────────────────────


class TestMaskWalk:
    def test_masks_direct_pii_keys(self):
        data = {
            "nodes": [
                {
                    "id": "person-1",
                    "label": "Person",
                    "firstname": "Max",
                    "lastname": "Mustermann",
                    "email": "max@example.com",
                    "role": "developer",  # not a PII key — untouched
                }
            ],
            "count": 1,
        }
        result = server._mask_walk(data, DEMO_KEYS)
        node = result["nodes"][0]
        assert node["firstname"] == "<PERSON>"
        assert node["lastname"] == "<PERSON>"
        assert node["email"] == "<EMAIL_ADDRESS>"
        assert node["role"] == "developer"
        assert node["id"] == "person-1"

    def test_unknown_keys_pass_through(self):
        data = {"nodes": [{"id": "p-1", "status": "active", "description": "A project"}]}
        result = server._mask_walk(data, DEMO_KEYS)
        assert result == data

    def test_case_insensitive_keys(self):
        data = {"FirstName": "Anna", "LASTNAME": "Müller", "Email": "a@b.de"}
        result = server._mask_walk(data, DEMO_KEYS)
        assert result["FirstName"] == "<PERSON>"
        assert result["LASTNAME"] == "<PERSON>"
        assert result["Email"] == "<EMAIL_ADDRESS>"

    def test_empty_strings_not_tagged(self):
        """Empty values stay empty — avoids rendering a bare <PERSON> for missing data."""
        data = {"name": "", "email": None}
        result = server._mask_walk(data, DEMO_KEYS)
        assert result["name"] == ""
        assert result["email"] is None

    def test_non_string_values_untouched(self):
        data = {"name": 42, "email": ["a@b.de"]}
        # 42 is not a string, so it passes through.
        # A list under `email` means the walker recurses into the list,
        # and each string inside the list is evaluated with its parent key
        # context *lost* — list elements don't inherit a key, so they pass
        # through too. Documenting this is intentional: graph properties
        # are flat scalars, not collections.
        result = server._mask_walk(data, DEMO_KEYS)
        assert result["name"] == 42
        assert result["email"] == ["a@b.de"]

    def test_recurses_into_nested_dicts(self):
        """Relationship rows come back as {a: <node>, r: <edge>, b: <node>}."""
        data = {
            "relationships": [
                {
                    "a": {"id": "p1", "label": "Person", "name": "Alice"},
                    "r": {"type": "OWNS"},
                    "b": {"id": "proj1", "label": "Project", "title": "Foo"},
                }
            ]
        }
        result = server._mask_walk(data, DEMO_KEYS)
        assert result["relationships"][0]["a"]["name"] == "<PERSON>"
        assert result["relationships"][0]["b"]["title"] == "Foo"  # title ≠ PII key

    def test_empty_input(self):
        assert server._mask_walk({}, DEMO_KEYS) == {}
        assert server._mask_walk([], DEMO_KEYS) == []

    def test_scalar_passthrough(self):
        assert server._mask_walk("hello", DEMO_KEYS) == "hello"
        assert server._mask_walk(42, DEMO_KEYS) == 42
        assert server._mask_walk(None, DEMO_KEYS) is None

    def test_empty_key_map_masks_nothing(self):
        """Deployers can disable graph masking by clearing the config section."""
        data = {"name": "Alice", "email": "a@b.de"}
        assert server._mask_walk(data, {}) == data

    def test_deterministic_for_non_english_names(self):
        """Regression: prior Presidio-based impl flagged 'Elena_Hartmann' as
        PERSON but 'Tim_Heller' as pass-through and 'Sarah_Bach' as LOCATION.
        With the config-driven walker, all three produce <PERSON>."""
        data = {"nodes": [
            {"name": "Elena_Hartmann"},
            {"name": "Tim_Heller"},
            {"name": "Sarah_Bach"},
        ]}
        result = server._mask_walk(data, DEMO_KEYS)
        for node in result["nodes"]:
            assert node["name"] == "<PERSON>"


# ── OPA loader ──────────────────────────────────────────────────────────────


class TestGetGraphPiiKeys:
    @pytest.mark.asyncio
    async def test_loads_from_opa(self):
        mock_http = AsyncMock()
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "result": {"name": "PERSON", "iban": "IBAN_CODE"}
        }
        mock_http.get.return_value = response

        with patch.object(server, "http", mock_http):
            keys = await server._get_graph_pii_keys()

        assert keys == {"name": "PERSON", "iban": "IBAN_CODE"}
        mock_http.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_caches_after_first_load(self):
        mock_http = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": {"name": "PERSON"}}
        mock_http.get.return_value = response

        with patch.object(server, "http", mock_http):
            await server._get_graph_pii_keys()
            await server._get_graph_pii_keys()
            await server._get_graph_pii_keys()

        mock_http.get.assert_called_once()  # not 3 times

    @pytest.mark.asyncio
    async def test_lowercases_keys(self):
        """Policy data may use any casing; the walker only checks lowercased keys."""
        mock_http = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "result": {"Name": "PERSON", "EMAIL": "EMAIL_ADDRESS"}
        }
        mock_http.get.return_value = response
        with patch.object(server, "http", mock_http):
            keys = await server._get_graph_pii_keys()
        assert "name" in keys and "email" in keys
        assert "Name" not in keys

    @pytest.mark.asyncio
    async def test_falls_back_when_opa_unreachable(self):
        mock_http = AsyncMock()
        mock_http.get.side_effect = Exception("connection refused")
        with patch.object(server, "http", mock_http):
            keys = await server._get_graph_pii_keys()
        # Non-empty fallback so the walker keeps protecting PII on OPA outage.
        assert "name" in keys
        assert keys["name"] == "PERSON"

    @pytest.mark.asyncio
    async def test_falls_back_when_config_empty(self):
        mock_http = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"result": {}}
        mock_http.get.return_value = response
        with patch.object(server, "http", mock_http):
            keys = await server._get_graph_pii_keys()
        # Empty OPA result → default, not silent "no protection".
        assert "name" in keys


# ── End-to-end integration of loader + walker ───────────────────────────────


class TestMaskGraphPii:
    @pytest.mark.asyncio
    async def test_end_to_end_with_opa(self):
        mock_http = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "result": {"name": "PERSON", "phone": "PHONE_NUMBER"}
        }
        mock_http.get.return_value = response

        with patch.object(server, "http", mock_http):
            result = await server._mask_graph_pii({
                "nodes": [{
                    "id": "e-1",
                    "label": "Employee",
                    "name": "Alice",
                    "phone": "+49 151 1234567",
                    "role": "Staff",
                }],
            })

        node = result["nodes"][0]
        assert node["name"] == "<PERSON>"
        assert node["phone"] == "<PHONE_NUMBER>"
        assert node["role"] == "Staff"
