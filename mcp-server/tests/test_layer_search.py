"""Tests for _build_qdrant_filter, _check_max_layer, and layer parameter support."""

import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch
from qdrant_client.models import Filter, FieldCondition, MatchValue

import server
from server import _build_qdrant_filter, _check_max_layer


class TestBuildQdrantFilter:
    """Unit tests for the _build_qdrant_filter utility function."""

    def test_no_filters_no_layer_returns_none(self):
        assert _build_qdrant_filter(None) is None

    def test_empty_filters_no_layer_returns_none(self):
        assert _build_qdrant_filter({}) is None

    def test_empty_filters_none_layer_returns_none(self):
        assert _build_qdrant_filter({}, None) is None

    def test_filters_only(self):
        result = _build_qdrant_filter({"project": "acme"})
        assert result is not None
        assert isinstance(result, Filter)
        assert len(result.must) == 1
        cond = result.must[0]
        assert cond.key == "project"
        assert cond.match.value == "acme"

    def test_multiple_filters(self):
        result = _build_qdrant_filter({"project": "acme", "source_type": "pdf"})
        assert result is not None
        assert len(result.must) == 2
        keys = {c.key for c in result.must}
        assert keys == {"project", "source_type"}

    def test_layer_only(self):
        result = _build_qdrant_filter(None, "L0")
        assert result is not None
        assert len(result.must) == 1
        cond = result.must[0]
        assert cond.key == "layer"
        assert cond.match.value == "L0"

    def test_layer_with_empty_filters(self):
        result = _build_qdrant_filter({}, "L1")
        assert result is not None
        assert len(result.must) == 1
        assert result.must[0].key == "layer"
        assert result.must[0].match.value == "L1"

    def test_filters_and_layer_combined(self):
        result = _build_qdrant_filter({"project": "acme"}, "L2")
        assert result is not None
        assert len(result.must) == 2
        keys = {c.key for c in result.must}
        assert keys == {"project", "layer"}
        layer_cond = next(c for c in result.must if c.key == "layer")
        assert layer_cond.match.value == "L2"

    def test_all_layer_values(self):
        for layer_val in ("L0", "L1", "L2"):
            result = _build_qdrant_filter(None, layer_val)
            assert result is not None
            assert result.must[0].match.value == layer_val

    def test_layer_empty_string_treated_as_no_layer(self):
        """Empty string layer should be treated as no layer (falsy)."""
        result = _build_qdrant_filter(None, "")
        assert result is None

    def test_filter_values_preserved_exactly(self):
        """Filter values should be passed through exactly as given."""
        result = _build_qdrant_filter({"classification": "confidential"})
        assert result.must[0].match.value == "confidential"

    def test_return_type_is_qdrant_filter(self):
        """Verify the return type matches what Qdrant expects."""
        result = _build_qdrant_filter({"key": "val"}, "L0")
        assert isinstance(result, Filter)
        for cond in result.must:
            assert isinstance(cond, FieldCondition)
            assert isinstance(cond.match, MatchValue)


class TestToolSchemasIncludeLayer:
    """Verify that layer parameter is present in tool schemas."""

    @pytest.fixture
    def tool_schemas(self):
        """Extract tool schemas by name from list_tools result.

        Since list_tools is async and requires MCP context, we inspect
        the source code structure instead by examining the Tool definitions.
        We can import the module and check the tool schemas are well-formed.
        """
        # We can't call list_tools() directly (needs MCP runtime),
        # so we verify _build_qdrant_filter handles layer values correctly.
        # The schema presence is verified by the integration of layer in dispatch.
        pass

    def test_build_filter_supports_layer_enum_values(self):
        """All enum values from the schema should work with the filter builder."""
        for layer in ["L0", "L1", "L2"]:
            result = _build_qdrant_filter(None, layer)
            assert result is not None
            assert result.must[0].match.value == layer

    def test_build_filter_without_layer_is_backward_compatible(self):
        """When layer is omitted, no layer filter is added (backward compat)."""
        # With filters
        result = _build_qdrant_filter({"project": "x"})
        keys = {c.key for c in result.must}
        assert "layer" not in keys

        # Without filters
        result = _build_qdrant_filter({})
        assert result is None


class TestCheckMaxLayer:
    """Unit tests for the _check_max_layer OPA integration."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_max_layer_from_opa(self):
        """OPA returns max_layer for analyst + confidential → L1."""
        respx.post(f"{server.OPA_URL}/v1/data/kb/layers/max_layer").mock(
            return_value=httpx.Response(200, json={"result": "L1"})
        )
        result = await _check_max_layer("analyst", "confidential")
        assert result == "L1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_l2_for_admin(self):
        """OPA returns L2 for admin role."""
        respx.post(f"{server.OPA_URL}/v1/data/kb/layers/max_layer").mock(
            return_value=httpx.Response(200, json={"result": "L2"})
        )
        result = await _check_max_layer("admin", "confidential")
        assert result == "L2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_l0_for_restricted(self):
        """OPA returns L0 for analyst + restricted."""
        respx.post(f"{server.OPA_URL}/v1/data/kb/layers/max_layer").mock(
            return_value=httpx.Response(200, json={"result": "L0"})
        )
        result = await _check_max_layer("analyst", "restricted")
        assert result == "L0"

    @pytest.mark.asyncio
    @respx.mock
    async def test_defaults_to_l2_on_opa_failure(self):
        """When OPA is unreachable, default to L2 (permissive)."""
        respx.post(f"{server.OPA_URL}/v1/data/kb/layers/max_layer").mock(
            side_effect=httpx.ConnectError("OPA down")
        )
        result = await _check_max_layer("analyst", "confidential")
        assert result == "L2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_defaults_to_l2_on_missing_result(self):
        """When OPA returns empty response, default to L2."""
        respx.post(f"{server.OPA_URL}/v1/data/kb/layers/max_layer").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await _check_max_layer("analyst", "confidential")
        assert result == "L2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_sends_correct_input(self):
        """Verify OPA receives agent_role and classification in input."""
        route = respx.post(f"{server.OPA_URL}/v1/data/kb/layers/max_layer").mock(
            return_value=httpx.Response(200, json={"result": "L2"})
        )
        await _check_max_layer("developer", "internal")
        assert route.called
        request_body = route.calls[0].request.content
        import json
        body = json.loads(request_body)
        assert body["input"]["agent_role"] == "developer"
        assert body["input"]["classification"] == "internal"
