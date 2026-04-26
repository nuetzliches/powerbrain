"""
PII middleware for the pb-proxy chat path.

Pseudonymizes user messages before the LLM call,
de-pseudonymizes LLM responses before returning to the user.
Tool call arguments are de-pseudonymized before MCP calls.
"""

import logging
import re
import secrets
from copy import deepcopy
from typing import Any

import httpx

import config

log = logging.getLogger("pb-proxy.pii")

# Regex for typed pseudonyms: [TYPE:8-hex-chars]
PII_PSEUDONYM_PATTERN = r"\[([A-Z_]+):([a-f0-9]{8})\]"


def generate_session_salt() -> str:
    """Generates a random salt for this request session."""
    return secrets.token_hex(16)


async def pseudonymize_messages(
    messages: list[dict[str, Any]],
    session_salt: str,
    http_client: httpx.AsyncClient,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    Pseudonymizes PII in all chat messages.

    Calls the ingestion service /pseudonymize for each message.
    Builds an aggregated reverse mapping across all messages.

    Returns:
        Tuple of (pseudonymized messages, reverse_map {pseudonym -> original})
    """
    reverse_map: dict[str, str] = {}
    result_messages = deepcopy(messages)

    for msg in result_messages:
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        try:
            resp = await http_client.post(
                f"{config.INGESTION_URL}/pseudonymize",
                json={"text": content, "salt": session_salt},
                headers=config.ingestion_headers(),
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("PII pseudonymization failed for message: %s", e)
            raise  # Caller decides fail-open/closed

        if data.get("contains_pii"):
            msg["content"] = data["text"]
            for original, pseudo in data.get("mapping", {}).items():
                reverse_map[pseudo] = original

    return result_messages, reverse_map


async def pseudonymize_tool_result(
    text: str,
    session_salt: str,
    http_client: httpx.AsyncClient,
    reverse_map: dict[str, str],
) -> str:
    """
    Pseudonymizes PII in a tool result string.

    Calls the ingestion service /pseudonymize and extends the reverse_map
    with new pseudonyms. Returns the pseudonymized text.
    On error, the original text is returned (fail-open for tool results,
    since aborting would destroy the entire conversation).
    """
    if not text or not text.strip():
        return text

    try:
        resp = await http_client.post(
            f"{config.INGESTION_URL}/pseudonymize",
            json={"text": text, "salt": session_salt},
            headers=config.ingestion_headers(),
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("PII pseudonymization of tool result failed: %s", e)
        return text  # fail-open: return original to avoid breaking the loop

    if data.get("contains_pii"):
        for original, pseudo in data.get("mapping", {}).items():
            reverse_map[pseudo] = original
        return data["text"]

    return text


def depseudonymize_text(text: str, reverse_map: dict[str, str]) -> str:
    """Replaces all pseudonyms in the text with the originals."""
    if not reverse_map:
        return text
    result = text
    for pseudo in sorted(reverse_map, key=len, reverse=True):
        result = result.replace(pseudo, reverse_map[pseudo])
    return result


def depseudonymize_tool_arguments(
    arguments: dict[str, Any], reverse_map: dict[str, str]
) -> dict[str, Any]:
    """De-pseudonymizes all string values in tool call arguments (recursive)."""
    if not reverse_map:
        return arguments
    result = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            result[key] = depseudonymize_text(value, reverse_map)
        elif isinstance(value, dict):
            result[key] = depseudonymize_tool_arguments(value, reverse_map)
        elif isinstance(value, list):
            result[key] = [
                depseudonymize_text(v, reverse_map) if isinstance(v, str)
                else depseudonymize_tool_arguments(v, reverse_map) if isinstance(v, dict)
                else v
                for v in value
            ]
        else:
            result[key] = value
    return result


def filter_non_text_content(
    messages: list[dict[str, Any]], action: str = "placeholder"
) -> tuple[list[dict[str, Any]], bool]:
    """
    Filters non-text content (images, files) from multimodal messages.

    Args:
        action: "block" (ValueError), "placeholder" (replace), "allow" (pass through)

    Returns:
        Tuple of (filtered messages, whether non-text content was found)
    """
    result = deepcopy(messages)
    had_non_text = False

    for msg in result:
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        has_non_text_parts = any(
            part.get("type") not in ("text",) for part in content
            if isinstance(part, dict)
        )
        if not has_non_text_parts:
            continue

        had_non_text = True

        if action == "block":
            raise ValueError(
                "Request contains non-text content (images/files) which cannot be "
                "scanned for PII. Blocked by policy."
            )
        elif action == "allow":
            continue
        else:  # placeholder
            msg["content"] = [
                part if part.get("type") == "text"
                else {"type": "text", "text": "[Non-text content removed — not scanned for PII by policy]"}
                for part in content
                if isinstance(part, dict)
            ]

    return result, had_non_text


async def vault_resolve_tool_result(
    text: str,
    *,
    purpose: str,
    mcp_url: str,
    mcp_token: str,
    http_client: httpx.AsyncClient,
) -> tuple[str, dict[str, int]]:
    """Call mcp-server's ``POST /vault/resolve`` to turn pseudonyms into originals.

    This is the enterprise feature toggle: a tool result like
    ``"Kundenprofil von [PERSON:a04de656]"`` comes back from MCP carrying
    pseudonyms that the proxy's in-memory ``pii_reverse_map`` never saw
    (the original was stored only in the vault, not in the chat request).
    Rather than surface ``[PERSON:…]`` to the downstream LLM — which
    wastes tokens and produces ugly answers — we ask mcp-server to do
    the vault lookup under a purpose-bound OPA decision.

    Fails open: on any error the original ``text`` comes back so the
    agent loop keeps running; stats describe how many pseudonyms were
    resolved for telemetry/demo.

    Returns ``(resolved_text, stats_dict)`` where stats has keys
    ``total`` / ``resolved`` / ``skipped``.
    """
    empty_stats = {"total": 0, "resolved": 0, "skipped": 0}
    if not text or "[" not in text:
        return text, empty_stats

    # Quick local match: skip the HTTP call entirely when no pseudonym
    # pattern is present. Keeps the hot path (most tool results have no
    # pseudonyms) free of extra network hops.
    if not re.search(PII_PSEUDONYM_PATTERN, text):
        return text, empty_stats

    try:
        resp = await http_client.post(
            f"{mcp_url.rstrip('/')}/vault/resolve",
            json={"text": text, "purpose": purpose},
            headers={"Authorization": f"Bearer {mcp_token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("vault_resolve_tool_result failed: %s", exc)
        return text, empty_stats

    stats = {
        "total":    int(data.get("total", 0)),
        "resolved": int(data.get("resolved", 0)),
        "skipped":  int(data.get("skipped", 0)),
    }
    return data.get("text", text), stats


def build_system_hint(entity_types: list[str]) -> str:
    """Generates a system prompt hint for the LLM."""
    if not entity_types:
        return ""
    types_str = ", ".join(f"[{t}:...]" for t in sorted(entity_types))
    return (
        "The following conversation contains typed pseudonyms "
        f"({types_str}). Treat them as normal names or values of their type. "
        "Use the pseudonyms exactly as given in your responses. "
        "Do not attempt to guess the originals."
    )
