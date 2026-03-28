"""
Bidirectional format translation between Anthropic Messages API
and the internal OpenAI format used by the agent loop.

Anthropic Messages API: https://docs.anthropic.com/en/api/messages
"""

import uuid
from typing import Any


# ── Anthropic → OpenAI (request ingress) ─────────────────────


def anthropic_messages_to_openai(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-format messages to OpenAI-format for the agent loop.

    Handles:
    - String content → kept as-is (both formats support it)
    - Content arrays with text/image blocks → joined text (simplified)
    - tool_use blocks in assistant messages → tool_calls array
    - tool_result blocks in user messages → separate tool role messages
    """
    result: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "assistant":
            result.extend(_convert_assistant_message(msg))
        elif role == "user":
            result.extend(_convert_user_message(msg))
        else:
            # system, etc. — pass through
            result.append({"role": role, "content": _flatten_content(content)})

    return result


def _convert_assistant_message(msg: dict) -> list[dict]:
    """Convert an Anthropic assistant message (possibly with tool_use blocks)."""
    content = msg.get("content", "")

    # Simple string content
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]

    # Content array: extract text + tool_use blocks
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": _to_json_string(block.get("input", {})),
                    },
                })

    result_msg: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        result_msg["tool_calls"] = tool_calls

    return [result_msg]


def _convert_user_message(msg: dict) -> list[dict]:
    """Convert an Anthropic user message (possibly with tool_result blocks)."""
    content = msg.get("content", "")

    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    # Separate tool_result blocks from regular content
    text_parts: list[str] = []
    tool_results: list[dict] = []

    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _flatten_content(block.get("content", "")),
                })
            elif block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "image":
                # Pass image blocks as-is for now (simplified)
                text_parts.append("[image]")

    result: list[dict] = []

    # Emit tool results first (OpenAI expects them right after assistant tool_calls)
    result.extend(tool_results)

    # Then the text content if any
    if text_parts:
        result.append({"role": "user", "content": "\n".join(text_parts)})

    return result if result else [{"role": "user", "content": ""}]


# ── OpenAI → Anthropic (response egress) ─────────────────────


def openai_response_to_anthropic(
    response_data: dict,
    model: str,
    input_tokens: int = 0,
) -> dict:
    """Convert an OpenAI chat completion response to Anthropic Messages format.

    Returns a dict matching the Anthropic Messages API response schema.
    """
    choice = {}
    if response_data.get("choices"):
        choice = response_data["choices"][0]

    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")

    # Build content array
    content: list[dict] = []

    # Text content
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # Tool calls → tool_use blocks
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        func = tc.get("function", {})
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": func.get("name", ""),
            "input": _parse_json_string(func.get("arguments", "{}")),
        })

    # Map finish reason
    stop_reason = _openai_to_anthropic_stop_reason(finish_reason)

    # Usage
    usage = response_data.get("usage", {})
    anthropic_usage = {
        "input_tokens": usage.get("prompt_tokens", input_tokens),
        "output_tokens": usage.get("completion_tokens", 0),
    }

    return {
        "id": _to_anthropic_id(response_data.get("id", "")),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


def openai_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Convert OpenAI function-calling tools to Anthropic tool format."""
    result = []
    for tool in tools:
        func = tool.get("function", {})
        result.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {"type": "object"}),
        })
    return result


# ── Anthropic SSE streaming ──────────────────────────────────


def format_anthropic_sse_message_start(
    msg_id: str, model: str, input_tokens: int = 0,
) -> dict:
    """Build the message_start event data."""
    return {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    }


def format_anthropic_sse_content_start(index: int, block_type: str = "text") -> dict:
    """Build a content_block_start event."""
    if block_type == "text":
        return {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        }
    # tool_use
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": "", "name": "", "input": {}},
    }


def format_anthropic_sse_text_delta(index: int, text: str) -> dict:
    """Build a content_block_delta for text."""
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def format_anthropic_sse_block_stop(index: int) -> dict:
    """Build a content_block_stop event."""
    return {"type": "content_block_stop", "index": index}


def format_anthropic_sse_message_delta(
    stop_reason: str = "end_turn", output_tokens: int = 0,
) -> dict:
    """Build a message_delta event."""
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }


# ── Helpers ──────────────────────────────────────────────────


def _flatten_content(content: Any) -> str:
    """Flatten content to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


def _to_json_string(obj: Any) -> str:
    """Ensure obj is a JSON string (OpenAI tool_calls expect string arguments)."""
    if isinstance(obj, str):
        return obj
    import json
    return json.dumps(obj)


def _parse_json_string(s: str) -> dict:
    """Parse a JSON string to dict (Anthropic tool_use expects dict input)."""
    if isinstance(s, dict):
        return s
    import json
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


def _openai_to_anthropic_stop_reason(finish_reason: str | None) -> str:
    """Map OpenAI finish_reason to Anthropic stop_reason."""
    mapping = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }
    return mapping.get(finish_reason or "stop", "end_turn")


def _to_anthropic_id(openai_id: str) -> str:
    """Convert an OpenAI response ID to Anthropic message ID format."""
    if openai_id.startswith("msg_"):
        return openai_id
    return f"msg_{uuid.uuid4().hex[:24]}"
