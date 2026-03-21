"""Tests for graph_service identifier validation and Cypher escaping."""

import pytest

from graph_service import validate_identifier, _require_identifier, _escape_cypher_value


class TestValidateIdentifier:
    def test_valid_simple(self):
        assert validate_identifier("name") is True

    def test_valid_underscore_prefix(self):
        assert validate_identifier("_private") is True

    def test_valid_with_digits(self):
        assert validate_identifier("col_2") is True

    def test_invalid_starts_with_digit(self):
        assert validate_identifier("2name") is False

    def test_invalid_hyphen(self):
        assert validate_identifier("my-col") is False

    def test_invalid_space(self):
        assert validate_identifier("my col") is False

    def test_invalid_semicolon_injection(self):
        assert validate_identifier("name; DROP TABLE") is False

    def test_invalid_cypher_injection(self):
        assert validate_identifier("n}) RETURN n//") is False

    def test_invalid_empty(self):
        assert validate_identifier("") is False

    def test_invalid_none(self):
        assert validate_identifier(None) is False

    def test_invalid_number(self):
        assert validate_identifier(42) is False


class TestRequireIdentifier:
    def test_valid_passes(self):
        _require_identifier("valid_name", "Label")  # should not raise

    def test_invalid_raises_valueerror(self):
        with pytest.raises(ValueError, match="Ungültiger Label"):
            _require_identifier("invalid-name", "Label")

    def test_context_in_error_message(self):
        with pytest.raises(ValueError, match="Property-Key"):
            _require_identifier("a b c", "Property-Key")


class TestEscapeCypherValue:
    def test_string(self):
        assert _escape_cypher_value("hello") == "'hello'"

    def test_string_with_single_quotes(self):
        result = _escape_cypher_value("it's")
        assert result == "'it\\'s'"

    def test_string_with_double_quotes(self):
        result = _escape_cypher_value('say "hi"')
        assert result == "'say \\\"hi\\\"'"

    def test_bool_true(self):
        assert _escape_cypher_value(True) == "true"

    def test_bool_false(self):
        assert _escape_cypher_value(False) == "false"

    def test_int(self):
        assert _escape_cypher_value(42) == "42"

    def test_float(self):
        assert _escape_cypher_value(3.14) == "3.14"

    def test_list(self):
        result = _escape_cypher_value(["a", "b"])
        assert result == "['a', 'b']"

    def test_bool_before_int(self):
        """bool is subclass of int in Python — must be checked first."""
        assert _escape_cypher_value(True) == "true"
        assert _escape_cypher_value(True) != "1"

    def test_non_primitive_converted_to_string(self):
        """Non-primitive types are wrapped as string."""
        result = _escape_cypher_value({"key": "val"})
        assert result.startswith("'")
        assert result.endswith("'")
