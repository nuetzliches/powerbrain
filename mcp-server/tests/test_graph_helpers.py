"""Tests for graph_service.py helper functions — Cypher parsing and escaping."""

from graph_service import _parse_return_columns, _escape_cypher_value


class TestParseReturnColumns:
    def test_single_return(self):
        assert _parse_return_columns("MATCH (n) RETURN n") == ["n"]

    def test_multiple_returns(self):
        result = _parse_return_columns("MATCH (a)-[r]->(b) RETURN a, r, b")
        assert result == ["a", "r", "b"]

    def test_return_with_alias(self):
        result = _parse_return_columns("MATCH (n) RETURN n AS node")
        assert result == ["node"]

    def test_return_distinct(self):
        result = _parse_return_columns("MATCH (n) RETURN DISTINCT n")
        assert result == ["n"]

    def test_no_return_clause(self):
        assert _parse_return_columns("CREATE (n:Foo {id: 'x'})") == ["result"]

    def test_multiple_with_aliases(self):
        result = _parse_return_columns(
            "MATCH (a)-[r]->(b) RETURN a AS source, r AS rel, b AS target"
        )
        assert result == ["source", "rel", "target"]

    def test_return_with_function(self):
        result = _parse_return_columns("MATCH (n) RETURN count(n) AS cnt")
        assert result == ["cnt"]

    def test_duplicate_aliases_deduplicated(self):
        result = _parse_return_columns("MATCH (n) RETURN n.name, n.name")
        # Should deduplicate with suffix
        assert len(result) == 2
        assert result[0] == "name"
        assert result[1].startswith("name")

    def test_return_with_limit_clause(self):
        # Regression: "RETURN a, r, b LIMIT 1" used to parse as three columns
        # ["a", "r", "b LIMIT 1"] and the third sanitised to "bLIMIT1",
        # producing a KeyError at row lookup. find_path uses this shape.
        result = _parse_return_columns(
            "MATCH (a)-[r*1..3]-(b) RETURN a, r, b LIMIT 1"
        )
        assert result == ["a", "r", "b"]

    def test_return_with_order_by(self):
        result = _parse_return_columns(
            "MATCH (n) RETURN n.name, n.score ORDER BY n.score DESC"
        )
        assert result == ["name", "score"]

    def test_return_with_skip_offset(self):
        result = _parse_return_columns("MATCH (n) RETURN n SKIP 5 LIMIT 10")
        assert result == ["n"]

    def test_return_with_offset_only(self):
        result = _parse_return_columns("MATCH (n) RETURN n OFFSET 5")
        assert result == ["n"]


class TestEscapeCypherValue:
    def test_string_escapes_quotes(self):
        result = _escape_cypher_value("it's a \"test\"")
        assert "\\'" in result
        assert '\\"' in result
        assert result.startswith("'") and result.endswith("'")

    def test_boolean_true(self):
        assert _escape_cypher_value(True) == "true"

    def test_boolean_false(self):
        assert _escape_cypher_value(False) == "false"

    def test_integer(self):
        assert _escape_cypher_value(42) == "42"

    def test_float(self):
        assert _escape_cypher_value(3.14) == "3.14"

    def test_list(self):
        result = _escape_cypher_value([1, "two", True])
        assert result.startswith("[")
        assert result.endswith("]")
        assert "1" in result
        assert "'two'" in result
        assert "true" in result

    def test_other_type_stringified(self):
        result = _escape_cypher_value(None)
        assert result == "'None'"
