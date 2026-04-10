"""Tests for pb-proxy/anthropic_format.py — bidirectional API format translation."""

import json

from anthropic_format import (
    anthropic_messages_to_openai,
    openai_response_to_anthropic,
    openai_tools_to_anthropic,
    format_anthropic_sse_message_start,
    format_anthropic_sse_content_start,
    format_anthropic_sse_text_delta,
    format_anthropic_sse_block_stop,
    format_anthropic_sse_message_delta,
    _flatten_content,
    _to_json_string,
    _parse_json_string,
    _openai_to_anthropic_stop_reason,
    _to_anthropic_id,
)


# ── Anthropic → OpenAI ────────────────────────────────────


class TestAnthropicToOpenai:
    def test_simple_string_message(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = anthropic_messages_to_openai(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_content_array_with_text_blocks(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ]}]
        result = anthropic_messages_to_openai(msgs)
        assert result == [{"role": "user", "content": "line1\nline2"}]

    def test_tool_use_blocks_to_tool_calls(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_123", "name": "search",
             "input": {"query": "test"}},
        ]}]
        result = anthropic_messages_to_openai(msgs)
        assert len(result) == 1
        msg = result[0]
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        tc = msg["tool_calls"][0]
        assert tc["id"] == "call_123"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "search"
        assert json.loads(tc["function"]["arguments"]) == {"query": "test"}

    def test_mixed_text_and_tool_use(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "Let me search."},
            {"type": "tool_use", "id": "c1", "name": "search", "input": {}},
        ]}]
        result = anthropic_messages_to_openai(msgs)
        msg = result[0]
        assert msg["content"] == "Let me search."
        assert len(msg["tool_calls"]) == 1

    def test_tool_result_blocks_to_tool_messages(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "found it"},
        ]}]
        result = anthropic_messages_to_openai(msgs)
        assert result == [{"role": "tool", "tool_call_id": "c1", "content": "found it"}]

    def test_image_block_placeholder(self):
        msgs = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64"}},
        ]}]
        result = anthropic_messages_to_openai(msgs)
        assert result == [{"role": "user", "content": "[image]"}]

    def test_system_role_passthrough(self):
        msgs = [{"role": "system", "content": "You are helpful."}]
        result = anthropic_messages_to_openai(msgs)
        assert result == [{"role": "system", "content": "You are helpful."}]

    def test_empty_content(self):
        msgs = [{"role": "user", "content": ""}]
        result = anthropic_messages_to_openai(msgs)
        assert result == [{"role": "user", "content": ""}]

    def test_assistant_string_content(self):
        msgs = [{"role": "assistant", "content": "Sure!"}]
        result = anthropic_messages_to_openai(msgs)
        assert result == [{"role": "assistant", "content": "Sure!"}]


# ── OpenAI → Anthropic ────────────────────────────────────


class TestOpenaiToAnthropic:
    def test_text_response(self):
        resp = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_response_to_anthropic(resp, model="gpt-4")
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["model"] == "gpt-4"
        assert result["content"] == [{"type": "text", "text": "Hello!"}]
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_tool_calls_response(self):
        resp = {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc",
                        "function": {"name": "search", "arguments": '{"q":"test"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }
        result = openai_response_to_anthropic(resp, model="gpt-4")
        assert result["stop_reason"] == "tool_use"
        assert len(result["content"]) == 1
        block = result["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "search"
        assert block["input"] == {"q": "test"}

    def test_stop_reason_mapping(self):
        for openai_reason, anthropic_reason in [
            ("stop", "end_turn"),
            ("tool_calls", "tool_use"),
            ("length", "max_tokens"),
            ("content_filter", "end_turn"),
            (None, "end_turn"),
        ]:
            assert _openai_to_anthropic_stop_reason(openai_reason) == anthropic_reason

    def test_usage_mapping(self):
        resp = {
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        result = openai_response_to_anthropic(resp, model="m")
        assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_empty_choices(self):
        resp = {"choices": [], "usage": {}}
        result = openai_response_to_anthropic(resp, model="m")
        assert result["content"] == []

    def test_existing_msg_id_preserved(self):
        assert _to_anthropic_id("msg_abc123") == "msg_abc123"

    def test_non_msg_id_gets_prefix(self):
        result = _to_anthropic_id("chatcmpl-xyz")
        assert result.startswith("msg_")

    def test_input_tokens_fallback(self):
        resp = {
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {},
        }
        result = openai_response_to_anthropic(resp, model="m", input_tokens=42)
        assert result["usage"]["input_tokens"] == 42


# ── Tool Conversion ───────────────────────────────────────


class TestToolConversion:
    def test_openai_tools_to_anthropic(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }]
        result = openai_tools_to_anthropic(tools)
        assert len(result) == 1
        assert result[0]["name"] == "search"
        assert result[0]["description"] == "Search the web"
        assert result[0]["input_schema"]["type"] == "object"

    def test_empty_tools_list(self):
        assert openai_tools_to_anthropic([]) == []


# ── SSE Formatters ────────────────────────────────────────


class TestSSEFormatters:
    def test_message_start(self):
        evt = format_anthropic_sse_message_start("msg_1", "claude-3", input_tokens=10)
        assert evt["type"] == "message_start"
        assert evt["message"]["id"] == "msg_1"
        assert evt["message"]["model"] == "claude-3"
        assert evt["message"]["usage"]["input_tokens"] == 10

    def test_content_start_text(self):
        evt = format_anthropic_sse_content_start(0, "text")
        assert evt["type"] == "content_block_start"
        assert evt["index"] == 0
        assert evt["content_block"]["type"] == "text"

    def test_content_start_tool_use(self):
        evt = format_anthropic_sse_content_start(1, "tool_use")
        assert evt["content_block"]["type"] == "tool_use"

    def test_text_delta(self):
        evt = format_anthropic_sse_text_delta(0, "Hello")
        assert evt["type"] == "content_block_delta"
        assert evt["delta"]["text"] == "Hello"

    def test_block_stop(self):
        evt = format_anthropic_sse_block_stop(0)
        assert evt == {"type": "content_block_stop", "index": 0}

    def test_message_delta(self):
        evt = format_anthropic_sse_message_delta("end_turn", output_tokens=42)
        assert evt["delta"]["stop_reason"] == "end_turn"
        assert evt["usage"]["output_tokens"] == 42


# ── Helpers ───────────────────────────────────────────────


class TestHelpers:
    def test_flatten_content_string(self):
        assert _flatten_content("hello") == "hello"

    def test_flatten_content_list(self):
        content = [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]
        assert _flatten_content(content) == "a\nb"

    def test_flatten_content_mixed_list(self):
        content = ["raw string", {"type": "text", "text": "block"}]
        assert _flatten_content(content) == "raw string\nblock"

    def test_flatten_content_other_type(self):
        assert _flatten_content(42) == "42"

    def test_to_json_string_from_dict(self):
        result = _to_json_string({"key": "val"})
        assert json.loads(result) == {"key": "val"}

    def test_to_json_string_from_string(self):
        assert _to_json_string('{"a":1}') == '{"a":1}'

    def test_parse_json_string_valid(self):
        assert _parse_json_string('{"a":1}') == {"a": 1}

    def test_parse_json_string_dict_passthrough(self):
        d = {"key": "val"}
        assert _parse_json_string(d) is d

    def test_parse_json_string_invalid(self):
        assert _parse_json_string("not json") == {}
