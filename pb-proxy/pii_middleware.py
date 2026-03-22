"""
PII-Middleware für den pb-proxy Chat-Pfad.

Pseudonymisiert User-Nachrichten vor dem LLM-Aufruf,
de-pseudonymisiert LLM-Antworten vor der Rückgabe an den User.
Tool-Call-Argumente werden vor MCP-Aufrufen de-pseudonymisiert.
"""

import logging
import re
import secrets
from copy import deepcopy
from typing import Any

import httpx

import config

log = logging.getLogger("pb-proxy.pii")

# Regex für typisierte Pseudonyme: [TYPE:8-hex-chars]
PII_PSEUDONYM_PATTERN = r"\[([A-Z_]+):([a-f0-9]{8})\]"


def generate_session_salt() -> str:
    """Erzeugt einen zufälligen Salt für diese Request-Session."""
    return secrets.token_hex(16)


async def pseudonymize_messages(
    messages: list[dict[str, Any]],
    session_salt: str,
    http_client: httpx.AsyncClient,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    Pseudonymisiert PII in allen Chat-Nachrichten.

    Ruft den Ingestion-Service /pseudonymize für jede Nachricht auf.
    Baut ein aggregiertes Reverse-Mapping über alle Nachrichten.

    Returns:
        Tuple aus (pseudonymisierte Messages, reverse_map {pseudonym -> original})
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


def depseudonymize_text(text: str, reverse_map: dict[str, str]) -> str:
    """Ersetzt alle Pseudonyme im Text durch die Originale."""
    if not reverse_map:
        return text
    result = text
    for pseudo in sorted(reverse_map, key=len, reverse=True):
        result = result.replace(pseudo, reverse_map[pseudo])
    return result


def depseudonymize_tool_arguments(
    arguments: dict[str, Any], reverse_map: dict[str, str]
) -> dict[str, Any]:
    """De-pseudonymisiert alle String-Werte in Tool-Call-Argumenten (rekursiv)."""
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
    Filtert non-text Content (Bilder, Dateien) aus multimodalen Messages.

    Args:
        action: "block" (ValueError), "placeholder" (ersetzen), "allow" (durchlassen)

    Returns:
        Tuple aus (gefilterte Messages, ob non-text Content gefunden wurde)
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


def build_system_hint(entity_types: list[str]) -> str:
    """Erzeugt einen System-Prompt-Hinweis für das LLM."""
    if not entity_types:
        return ""
    types_str = ", ".join(f"[{t}:...]" for t in sorted(entity_types))
    return (
        "Die folgende Konversation enthält typisierte Pseudonyme "
        f"({types_str}). Behandle sie als normale Namen bzw. Werte ihres Typs. "
        "Verwende die Pseudonyme exakt so wie angegeben in deinen Antworten. "
        "Versuche nicht, die Originale zu erraten."
    )
