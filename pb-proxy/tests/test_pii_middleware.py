"""Tests für die PII-Middleware des Proxy."""
import pytest
import re
import sys
import os
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path so we can import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pii_middleware import (
    pseudonymize_messages,
    depseudonymize_text,
    depseudonymize_tool_arguments,
    filter_non_text_content,
    build_system_hint,
    PII_PSEUDONYM_PATTERN,
)


class TestPseudonymizeMessages:
    @pytest.mark.asyncio
    async def test_pseudonymizes_user_messages(self):
        messages = [
            {"role": "system", "content": "Du bist ein Assistent."},
            {"role": "user", "content": "Sebastian braucht Hilfe"},
        ]
        mock_response_system = MagicMock(
            status_code=200,
            json=lambda: {"text": "Du bist ein Assistent.", "mapping": {}, "contains_pii": False, "entity_types": []},
            raise_for_status=lambda: None,
        )
        mock_response_user = MagicMock(
            status_code=200,
            json=lambda: {
                "text": "[PERSON:a1b2c3d4] braucht Hilfe",
                "mapping": {"Sebastian": "[PERSON:a1b2c3d4]"},
                "contains_pii": True,
                "entity_types": ["PERSON"],
            },
            raise_for_status=lambda: None,
        )
        http = AsyncMock()
        http.post = AsyncMock(side_effect=[mock_response_system, mock_response_user])

        result_messages, reverse_map = await pseudonymize_messages(
            messages, session_salt="test-salt", http_client=http
        )

        assert result_messages[0]["content"] == "Du bist ein Assistent."
        assert result_messages[1]["content"] == "[PERSON:a1b2c3d4] braucht Hilfe"
        assert reverse_map == {"[PERSON:a1b2c3d4]": "Sebastian"}

    @pytest.mark.asyncio
    async def test_no_pii_no_changes(self):
        messages = [{"role": "user", "content": "Hallo Welt"}]
        http = AsyncMock()
        http.post = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {"text": "Hallo Welt", "mapping": {}, "contains_pii": False, "entity_types": []},
            raise_for_status=lambda: None,
        ))

        result_messages, reverse_map = await pseudonymize_messages(
            messages, session_salt="salt", http_client=http
        )

        assert result_messages[0]["content"] == "Hallo Welt"
        assert reverse_map == {}

    @pytest.mark.asyncio
    async def test_skips_non_string_content(self):
        """Messages with list content (multimodal) are skipped."""
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        http = AsyncMock()

        result_messages, reverse_map = await pseudonymize_messages(
            messages, session_salt="salt", http_client=http
        )

        # http.post should NOT be called for non-string content
        http.post.assert_not_called()
        assert reverse_map == {}

    @pytest.mark.asyncio
    async def test_raises_on_ingestion_failure(self):
        messages = [{"role": "user", "content": "Sebastian test"}]
        http = AsyncMock()
        http.post = AsyncMock(side_effect=Exception("connection refused"))

        with pytest.raises(Exception, match="connection refused"):
            await pseudonymize_messages(messages, session_salt="salt", http_client=http)


class TestDepseudonymizeText:
    def test_replaces_pseudonyms(self):
        text = "[PERSON:a1b2c3d4] sollte den Termin bestätigen."
        reverse_map = {"[PERSON:a1b2c3d4]": "Sebastian"}
        assert depseudonymize_text(text, reverse_map) == "Sebastian sollte den Termin bestätigen."

    def test_multiple_pseudonyms(self):
        text = "[PERSON:a1b2c3d4] und [PERSON:e5f6a7b8] haben ein Meeting."
        reverse_map = {
            "[PERSON:a1b2c3d4]": "Sebastian",
            "[PERSON:e5f6a7b8]": "Maria",
        }
        result = depseudonymize_text(text, reverse_map)
        assert result == "Sebastian und Maria haben ein Meeting."

    def test_empty_map_no_change(self):
        assert depseudonymize_text("Keine PII hier.", {}) == "Keine PII hier."


class TestDepseudonymizeToolArguments:
    def test_replaces_in_string_values(self):
        arguments = {"query": "Tickets von [PERSON:a1b2c3d4]"}
        reverse_map = {"[PERSON:a1b2c3d4]": "Sebastian"}
        result = depseudonymize_tool_arguments(arguments, reverse_map)
        assert result["query"] == "Tickets von Sebastian"

    def test_nested_dicts(self):
        arguments = {"conditions": {"name": "[PERSON:a1b2c3d4]"}, "limit": 10}
        reverse_map = {"[PERSON:a1b2c3d4]": "Sebastian"}
        result = depseudonymize_tool_arguments(arguments, reverse_map)
        assert result["conditions"]["name"] == "Sebastian"
        assert result["limit"] == 10

    def test_list_values(self):
        arguments = {"names": ["[PERSON:a1b2c3d4]", "[PERSON:e5f6a7b8]"]}
        reverse_map = {"[PERSON:a1b2c3d4]": "Sebastian", "[PERSON:e5f6a7b8]": "Maria"}
        result = depseudonymize_tool_arguments(arguments, reverse_map)
        assert result["names"] == ["Sebastian", "Maria"]

    def test_non_string_values_unchanged(self):
        result = depseudonymize_tool_arguments({"limit": 50, "flag": True}, {})
        assert result == {"limit": 50, "flag": True}


class TestFilterNonTextContent:
    def test_text_only_unchanged(self):
        messages = [{"role": "user", "content": "Hallo Welt"}]
        result, had_non_text = filter_non_text_content(messages, action="placeholder")
        assert result == messages
        assert had_non_text is False

    def test_multimodal_placeholder(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Was zeigt das Bild?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]}]
        result, had_non_text = filter_non_text_content(messages, action="placeholder")
        assert had_non_text is True
        parts = result[0]["content"]
        assert parts[0] == {"type": "text", "text": "Was zeigt das Bild?"}
        assert parts[1]["type"] == "text"
        assert "PII" in parts[1]["text"]

    def test_multimodal_block(self):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]}]
        with pytest.raises(ValueError, match="non-text"):
            filter_non_text_content(messages, action="block")

    def test_multimodal_allow(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Schau mal"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]}]
        result, had_non_text = filter_non_text_content(messages, action="allow")
        assert result == messages
        assert had_non_text is True


class TestBuildSystemHint:
    def test_returns_hint_with_entity_types(self):
        hint = build_system_hint(["PERSON", "EMAIL_ADDRESS"])
        assert "PERSON" in hint
        assert "[" in hint

    def test_empty_types_returns_empty(self):
        assert build_system_hint([]) == ""


class TestPseudonymPattern:
    def test_regex_matches_typed_pseudonyms(self):
        text = "Hallo [PERSON:a1b2c3d4] und [EMAIL_ADDRESS:f9e8d7c6]!"
        matches = re.findall(PII_PSEUDONYM_PATTERN, text)
        assert len(matches) == 2
