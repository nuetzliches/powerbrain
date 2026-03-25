"""Tests for _build_qdrant_filter with layer parameter support."""

import pytest
from qdrant_client.models import Filter

from server import _build_qdrant_filter


class TestBuildQdrantFilterLayerSupport:
    """Verify layer param is correctly added to Qdrant filter conditions."""

    def test_no_args_returns_none(self):
        assert _build_qdrant_filter(None) is None
        assert _build_qdrant_filter({}) is None
        assert _build_qdrant_filter({}, None) is None

    def test_layer_only(self):
        result = _build_qdrant_filter(None, "L0")
        assert isinstance(result, Filter)
        assert len(result.must) == 1
        assert result.must[0].key == "layer"
        assert result.must[0].match.value == "L0"

    def test_filters_only(self):
        result = _build_qdrant_filter({"project": "acme"})
        assert isinstance(result, Filter)
        assert len(result.must) == 1
        assert result.must[0].key == "project"

    def test_filters_and_layer_combined(self):
        result = _build_qdrant_filter({"project": "acme"}, "L2")
        assert len(result.must) == 2
        keys = {c.key for c in result.must}
        assert keys == {"project", "layer"}

    @pytest.mark.parametrize("layer", ["L0", "L1", "L2"])
    def test_all_layer_values(self, layer):
        result = _build_qdrant_filter(None, layer)
        assert result.must[0].match.value == layer

    def test_empty_string_layer_treated_as_no_layer(self):
        result = _build_qdrant_filter({"project": "x"}, "")
        assert len(result.must) == 1
        assert result.must[0].key == "project"