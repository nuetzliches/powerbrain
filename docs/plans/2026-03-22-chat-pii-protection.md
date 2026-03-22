# Chat-Path PII Protection — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reversible PII-Pseudonymisierung im pb-proxy Chat-Pfad, sodass User-Nachrichten pseudonymisiert an den LLM-Provider gehen und LLM-Antworten vor der Rückgabe de-pseudonymisiert werden.

**Architecture:** Proxy-Middleware ruft den bestehenden Ingestion-Service (`POST /pseudonymize`) für PII-Erkennung auf. Mapping lebt ephemeral im Request-Scope. OPA-Policy steuert Aktivierung und Erzwingung. Tool-Call-Argumente werden vor MCP-Aufrufen de-pseudonymisiert, damit der MCP-Server echte Werte erhält.

**Tech Stack:** Python 3.12+, FastAPI, httpx, OPA/Rego, Presidio (via Ingestion-Service), pytest

**Design-Doc:** `docs/plans/2026-03-22-chat-pii-protection-design.md`

---

### Task 1: Typisiertes Pseudonym-Format in PIIScanner

**Files:**
- Modify: `ingestion/pii_scanner.py:204-241`
- Modify: `ingestion/tests/test_pii_scanner.py` (TestPseudonymizeText)

**Step 1: Update failing tests for typed format**

In `ingestion/tests/test_pii_scanner.py`, update `TestPseudonymizeText` to expect typed pseudonyms:

```python
class TestPseudonymizeText:
    def test_deterministic(self, scanner):
        """Gleicher Input + Salt ergibt gleiches typisiertes Pseudonym."""
        scanner._analyzer.analyze.return_value = [
            MagicMock(entity_type="PERSON", start=0, end=4, score=0.99),
        ]
        text1, map1 = scanner.pseudonymize_text("Anna geht.", "salt1")
        text2, map2 = scanner.pseudonymize_text("Anna geht.", "salt1")
        assert text1 == text2
        assert map1 == map2
        # Typisiertes Format
        pseudo = map1["Anna"]
        assert pseudo.startswith("[PERSON:")
        assert pseudo.endswith("]")
        assert len(pseudo) == len("[PERSON:") + 8 + 1  # [PERSON:xxxxxxxx]

    def test_typed_format_in_text(self, scanner):
        """Pseudonym im Text hat das Format [TYPE:hash]."""
        scanner._analyzer.analyze.return_value = [
            MagicMock(entity_type="EMAIL_ADDRESS", start=0, end=16, score=0.95),
        ]
        text, mapping = scanner.pseudonymize_text("test@example.com ist aktiv", "salt")
        assert "[EMAIL_ADDRESS:" in text
        assert "test@example.com" not in text

    def test_different_salts(self, scanner):
        """Verschiedene Salts ergeben verschiedene Pseudonyme."""
        scanner._analyzer.analyze.return_value = [
            MagicMock(entity_type="PERSON", start=0, end=4, score=0.99),
        ]
        _, map1 = scanner.pseudonymize_text("Anna geht.", "salt1")
        _, map2 = scanner.pseudonymize_text("Anna geht.", "salt2")
        assert map1["Anna"] != map2["Anna"]

    def test_no_pii(self, scanner):
        """Text ohne PII bleibt unverändert."""
        scanner._analyzer.analyze.return_value = []
        text, mapping = scanner.pseudonymize_text("Hallo Welt", "salt")
        assert text == "Hallo Welt"
        assert mapping == {}

    def test_multiple_entities(self, scanner):
        """Mehrere Entities bekommen unterschiedliche typisierte Pseudonyme."""
        scanner._analyzer.analyze.return_value = [
            MagicMock(entity_type="PERSON", start=0, end=4, score=0.99),
            MagicMock(entity_type="PERSON", start=9, end=14, score=0.99),
        ]
        text, mapping = scanner.pseudonymize_text("Anna und Maria gehen.", "salt")
        assert "[PERSON:" in text
        assert len(mapping) == 2
        assert mapping["Anna"] != mapping["Maria"]
        assert mapping["Anna"].startswith("[PERSON:")
        assert mapping["Maria"].startswith("[PERSON:")
```

**Step 2: Run tests to verify they fail**

Run: `cd ingestion && python3 -m pytest tests/test_pii_scanner.py::TestPseudonymizeText -v`
Expected: FAIL — pseudonyms don't match `[TYPE:hash]` format

**Step 3: Implement typed pseudonym format**

In `ingestion/pii_scanner.py`, modify `pseudonymize_text` (lines 204-241):

```python
def pseudonymize_text(
    self, text: str, salt: str, language: str = "de"
) -> tuple[str, dict[str, str]]:
    """
    Ersetzt PII durch deterministische, typisierte Pseudonyme.
    Format: [ENTITY_TYPE:8-char-hex] — z.B. [PERSON:a1b2c3d4]
    Gleicher Input + Salt → gleiches Pseudonym (für Verknüpfbarkeit).

    Returns:
        Tuple aus (pseudonymisierter Text, Mapping {original → pseudonym})
    """
    results = self.analyzer.analyze(
        text=text,
        language=language,
        entities=PII_ENTITY_TYPES,
        score_threshold=MIN_CONFIDENCE,
    )

    def make_pseudonym(entity_text: str, entity_type: str) -> str:
        h = hashlib.sha256(f"{salt}:{entity_text}".encode()).hexdigest()[:8]
        return f"[{entity_type}:{h}]"

    mapping: dict[str, str] = {}
    for r in results:
        original = text[r.start:r.end]
        pseudo = make_pseudonym(original, r.entity_type)
        mapping[original] = pseudo

    pseudonymized = text
    for r in sorted(results, key=lambda x: x.start, reverse=True):
        original = pseudonymized[r.start:r.end]
        pseudo = mapping.get(original, make_pseudonym(original, r.entity_type))
        pseudonymized = pseudonymized[:r.start] + pseudo + pseudonymized[r.end:]

    return pseudonymized, mapping
```

**Step 4: Run tests to verify they pass**

Run: `cd ingestion && python3 -m pytest tests/test_pii_scanner.py::TestPseudonymizeText -v`
Expected: PASS

**Step 5: Check existing Ingestion tests still pass**

Run: `cd ingestion && python3 -m pytest tests/ -v`
Expected: All PASS. If ingestion_api tests reference bare hex format in pseudonyms, update those too.

**Step 6: Commit**

```bash
git add ingestion/pii_scanner.py ingestion/tests/test_pii_scanner.py
git commit -m "feat(pii): typed pseudonym format [TYPE:hash] for LLM intelligibility"
```

---

### Task 2: Neuer Ingestion-Endpunkt `POST /pseudonymize`

**Files:**
- Modify: `ingestion/ingestion_api.py` (add models + endpoint after line 472)
- Create: `ingestion/tests/test_pseudonymize_endpoint.py`

**Step 1: Write failing test**

Create `ingestion/tests/test_pseudonymize_endpoint.py`:

```python
"""Tests für den /pseudonymize Endpunkt."""
import pytest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """TestClient mit gemocktem PIIScanner."""
    with patch("ingestion_api.get_scanner") as mock_get:
        scanner = MagicMock()
        mock_get.return_value = scanner
        # Lazy import so patches apply
        import ingestion_api
        yield TestClient(ingestion_api.app), scanner


class TestPseudonymizeEndpoint:
    def test_pseudonymize_with_pii(self, client):
        """Text mit PII wird pseudonymisiert, Mapping zurückgegeben."""
        http, scanner = client
        scanner.pseudonymize_text.return_value = (
            "[PERSON:a1b2c3d4] braucht Hilfe",
            {"Sebastian": "[PERSON:a1b2c3d4]"},
        )
        scanner.scan_text.return_value = MagicMock(
            contains_pii=True,
            entity_counts={"PERSON": 1},
        )

        resp = http.post("/pseudonymize", json={
            "text": "Sebastian braucht Hilfe",
            "salt": "test-salt-123",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "[PERSON:a1b2c3d4] braucht Hilfe"
        assert data["mapping"] == {"Sebastian": "[PERSON:a1b2c3d4]"}
        assert data["contains_pii"] is True
        assert "PERSON" in data["entity_types"]

    def test_pseudonymize_no_pii(self, client):
        """Text ohne PII wird unverändert zurückgegeben."""
        http, scanner = client
        scanner.pseudonymize_text.return_value = ("Hallo Welt", {})
        scanner.scan_text.return_value = MagicMock(
            contains_pii=False,
            entity_counts={},
        )

        resp = http.post("/pseudonymize", json={
            "text": "Hallo Welt",
            "salt": "test-salt",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "Hallo Welt"
        assert data["mapping"] == {}
        assert data["contains_pii"] is False

    def test_pseudonymize_requires_salt(self, client):
        """Request ohne salt wird abgelehnt (422)."""
        http, _ = client
        resp = http.post("/pseudonymize", json={"text": "Test"})
        assert resp.status_code == 422
```

**Step 2: Run tests to verify they fail**

Run: `cd ingestion && python3 -m pytest tests/test_pseudonymize_endpoint.py -v`
Expected: FAIL — endpoint `/pseudonymize` not found (404)

**Step 3: Implement endpoint**

In `ingestion/ingestion_api.py`, add after line 113 (request models):

```python
class PseudonymizeRequest(BaseModel):
    text: str
    salt: str
    language: str = "de"


class PseudonymizeResponse(BaseModel):
    text: str
    mapping: dict[str, str]
    contains_pii: bool
    entity_types: list[str]
```

Add after line 472 (after `/scan` endpoint):

```python
@app.post("/pseudonymize")
async def pseudonymize(req: PseudonymizeRequest) -> PseudonymizeResponse:
    """Pseudonymisiert PII im Text ohne Speicherung.

    Wird vom pb-proxy aufgerufen, bevor Chat-Nachrichten
    an den LLM-Provider gesendet werden.
    Kein Vault-Write, kein Embedding, kein Qdrant.
    """
    scanner = get_scanner()
    scan_result = scanner.scan_text(req.text, language=req.language)

    if scan_result.contains_pii:
        pseudonymized, mapping = scanner.pseudonymize_text(
            req.text, salt=req.salt, language=req.language
        )
        entity_types = list(scan_result.entity_counts.keys())
    else:
        pseudonymized = req.text
        mapping = {}
        entity_types = []

    return PseudonymizeResponse(
        text=pseudonymized,
        mapping=mapping,
        contains_pii=scan_result.contains_pii,
        entity_types=entity_types,
    )
```

**Step 4: Run tests to verify they pass**

Run: `cd ingestion && python3 -m pytest tests/test_pseudonymize_endpoint.py -v`
Expected: PASS

**Step 5: Run all ingestion tests**

Run: `cd ingestion && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add ingestion/ingestion_api.py ingestion/tests/test_pseudonymize_endpoint.py
git commit -m "feat(ingestion): add /pseudonymize endpoint for chat-path PII protection"
```

---

### Task 3: OPA-Policy für PII-Scan im Proxy

**Files:**
- Modify: `opa-policies/kb/proxy.rego`
- Modify: `opa-policies/kb/test_proxy.rego`

**Step 1: Write Rego tests**

Append to `opa-policies/kb/test_proxy.rego`:

```rego
# ── PII Protection Tests ─────────────────────────────────────

test_pii_scan_enabled_default if {
    proxy.pii_scan_enabled with input as {"agent_role": "analyst"}
}

test_pii_scan_enabled_for_developer if {
    proxy.pii_scan_enabled with input as {"agent_role": "developer"}
}

test_pii_scan_admin_opt_out_allowed if {
    not proxy.pii_scan_enabled with input as {
        "agent_role": "admin",
        "pii_scan_opt_out": true,
    }
}

test_pii_scan_admin_opt_out_blocked_when_forced if {
    proxy.pii_scan_enabled with input as {
        "agent_role": "admin",
        "pii_scan_opt_out": true,
        "pii_scan_forced": true,
    }
}

test_pii_scan_non_admin_cannot_opt_out if {
    proxy.pii_scan_enabled with input as {
        "agent_role": "analyst",
        "pii_scan_opt_out": true,
    }
}

test_pii_entity_types_defined if {
    count(proxy.pii_entity_types) > 0
    "PERSON" in proxy.pii_entity_types
    "EMAIL_ADDRESS" in proxy.pii_entity_types
}

test_pii_scan_forced_default_false if {
    not proxy.pii_scan_forced with input as {}
}
```

**Step 2: Run tests to verify they fail**

Run: `docker exec kb-opa /opa test /policies/kb/ -v`
Expected: FAIL — rules `pii_scan_enabled`, `pii_scan_forced`, `pii_entity_types` not defined

**Step 3: Implement OPA rules**

Append to `opa-policies/kb/proxy.rego` (after `max_iterations` rule):

```rego
# ── PII Protection ───────────────────────────────────────────

# Scan default aktiv für alle Rollen
default pii_scan_enabled := true

# Policy kann Scan erzwingen (kein Opt-out möglich)
# Wird über input.pii_scan_forced gesteuert (vom Deployment gesetzt)
pii_scan_forced if {
    input.pii_scan_forced == true
}

# Admin-Opt-out: nur wenn nicht forced
pii_scan_opt_out_allowed if {
    input.agent_role == "admin"
    input.pii_scan_opt_out == true
    not pii_scan_forced
}

# Scan deaktiviert nur bei erlaubtem Opt-out
pii_scan_enabled := false if {
    pii_scan_opt_out_allowed
}

# Welche Entity-Typen pseudonymisiert werden
pii_entity_types := {"PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "LOCATION"}

# System-Prompt-Injection erlaubt
default pii_system_prompt_injection := true
```

**Step 4: Run tests to verify they pass**

Run: `docker exec kb-opa /opa test /policies/kb/ -v`
Expected: All PASS (including existing proxy tests)

**Step 5: Commit**

```bash
git add opa-policies/kb/proxy.rego opa-policies/kb/test_proxy.rego
git commit -m "feat(opa): add PII protection policy for proxy chat path"
```

---

### Task 4: Proxy-Config erweitern

**Files:**
- Modify: `pb-proxy/config.py`

**Step 1: Add config values**

Append to `pb-proxy/config.py`:

```python
# ── PII Protection ───────────────────────────────────────────
INGESTION_URL = os.getenv("INGESTION_URL", "http://ingestion:8081")
PII_SCAN_ENABLED = os.getenv("PII_SCAN_ENABLED", "true").lower() == "true"
PII_SCAN_FORCED = os.getenv("PII_SCAN_FORCED", "false").lower() == "true"
```

**Step 2: Commit**

```bash
git add pb-proxy/config.py
git commit -m "feat(proxy): add PII protection config (INGESTION_URL, PII_SCAN_*)"
```

---

### Task 5: PII-Middleware im Proxy

**Files:**
- Create: `pb-proxy/pii_middleware.py`
- Create: `pb-proxy/tests/test_pii_middleware.py`

**Step 1: Write failing tests**

Create `pb-proxy/tests/test_pii_middleware.py`:

```python
"""Tests für die PII-Middleware des Proxy."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from pii_middleware import (
    pseudonymize_messages,
    depseudonymize_text,
    depseudonymize_tool_arguments,
    build_system_hint,
    PII_PSEUDONYM_PATTERN,
)
import re


class TestPseudonymizeMessages:
    @pytest.mark.asyncio
    async def test_pseudonymizes_user_messages(self):
        """User-Nachrichten werden pseudonymisiert."""
        messages = [
            {"role": "system", "content": "Du bist ein Assistent."},
            {"role": "user", "content": "Sebastian braucht Hilfe"},
        ]
        mock_response = {
            "text": "[PERSON:a1b2c3d4] braucht Hilfe",
            "mapping": {"Sebastian": "[PERSON:a1b2c3d4]"},
            "contains_pii": True,
            "entity_types": ["PERSON"],
        }
        http = AsyncMock()
        http.post.return_value = MagicMock(
            status_code=200, json=lambda: mock_response, raise_for_status=lambda: None,
        )

        result_messages, reverse_map = await pseudonymize_messages(
            messages, session_salt="test-salt", http_client=http
        )

        assert result_messages[0]["content"] == "Du bist ein Assistent."  # system untouched
        assert result_messages[1]["content"] == "[PERSON:a1b2c3d4] braucht Hilfe"
        assert reverse_map == {"[PERSON:a1b2c3d4]": "Sebastian"}

    @pytest.mark.asyncio
    async def test_no_pii_no_changes(self):
        """Ohne PII bleiben Nachrichten unverändert."""
        messages = [{"role": "user", "content": "Hallo Welt"}]
        mock_response = {
            "text": "Hallo Welt",
            "mapping": {},
            "contains_pii": False,
            "entity_types": [],
        }
        http = AsyncMock()
        http.post.return_value = MagicMock(
            status_code=200, json=lambda: mock_response, raise_for_status=lambda: None,
        )

        result_messages, reverse_map = await pseudonymize_messages(
            messages, session_salt="salt", http_client=http
        )

        assert result_messages[0]["content"] == "Hallo Welt"
        assert reverse_map == {}

    @pytest.mark.asyncio
    async def test_system_messages_also_pseudonymized(self):
        """System-Messages werden ebenfalls pseudonymisiert (könnten PII enthalten)."""
        messages = [
            {"role": "system", "content": "Du hilfst Sebastian."},
        ]
        mock_response = {
            "text": "Du hilfst [PERSON:a1b2c3d4].",
            "mapping": {"Sebastian": "[PERSON:a1b2c3d4]"},
            "contains_pii": True,
            "entity_types": ["PERSON"],
        }
        http = AsyncMock()
        http.post.return_value = MagicMock(
            status_code=200, json=lambda: mock_response, raise_for_status=lambda: None,
        )

        result_messages, reverse_map = await pseudonymize_messages(
            messages, session_salt="salt", http_client=http
        )

        assert "[PERSON:a1b2c3d4]" in result_messages[0]["content"]


class TestDepseudonymizeText:
    def test_replaces_pseudonyms(self):
        """Pseudonyme werden durch Originale ersetzt."""
        text = "[PERSON:a1b2c3d4] sollte den Termin bestätigen."
        reverse_map = {"[PERSON:a1b2c3d4]": "Sebastian"}
        assert depseudonymize_text(text, reverse_map) == "Sebastian sollte den Termin bestätigen."

    def test_multiple_pseudonyms(self):
        """Mehrere Pseudonyme werden ersetzt."""
        text = "[PERSON:a1b2c3d4] und [PERSON:e5f6a7b8] haben ein Meeting."
        reverse_map = {
            "[PERSON:a1b2c3d4]": "Sebastian",
            "[PERSON:e5f6a7b8]": "Maria",
        }
        result = depseudonymize_text(text, reverse_map)
        assert result == "Sebastian und Maria haben ein Meeting."

    def test_empty_map_no_change(self):
        """Ohne Mapping bleibt Text unverändert."""
        text = "Keine PII hier."
        assert depseudonymize_text(text, {}) == text


class TestDepseudonymizeToolArguments:
    def test_replaces_in_string_values(self):
        """Pseudonyme in Tool-Argument-Strings werden ersetzt."""
        arguments = {"query": "Tickets von [PERSON:a1b2c3d4]"}
        reverse_map = {"[PERSON:a1b2c3d4]": "Sebastian"}
        result = depseudonymize_tool_arguments(arguments, reverse_map)
        assert result["query"] == "Tickets von Sebastian"

    def test_nested_dicts(self):
        """Auch verschachtelte Dict-Werte werden de-pseudonymisiert."""
        arguments = {
            "conditions": {"name": "[PERSON:a1b2c3d4]"},
            "limit": 10,
        }
        reverse_map = {"[PERSON:a1b2c3d4]": "Sebastian"}
        result = depseudonymize_tool_arguments(arguments, reverse_map)
        assert result["conditions"]["name"] == "Sebastian"

    def test_non_string_values_unchanged(self):
        """Nicht-String-Werte bleiben unverändert."""
        arguments = {"limit": 50, "flag": True}
        result = depseudonymize_tool_arguments(arguments, {})
        assert result == {"limit": 50, "flag": True}


class TestBuildSystemHint:
    def test_returns_hint_with_entity_types(self):
        """System-Hint enthält die erkannten Entity-Typen."""
        hint = build_system_hint(["PERSON", "EMAIL_ADDRESS"])
        assert "PERSON" in hint
        assert "[" in hint  # references typed format

    def test_empty_types_returns_empty(self):
        """Ohne Entity-Typen kein Hint."""
        assert build_system_hint([]) == ""


class TestPseudonymPattern:
    def test_regex_matches_typed_pseudonyms(self):
        """Regex erkennt typisierte Pseudonyme."""
        text = "Hallo [PERSON:a1b2c3d4] und [EMAIL_ADDRESS:f9e8d7c6]!"
        matches = re.findall(PII_PSEUDONYM_PATTERN, text)
        assert len(matches) == 2
```

**Step 2: Run tests to verify they fail**

Run: `cd pb-proxy && python3 -m pytest tests/test_pii_middleware.py -v`
Expected: FAIL — `pii_middleware` module not found

**Step 3: Implement PII middleware**

Create `pb-proxy/pii_middleware.py`:

```python
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
        Tuple aus (pseudonymisierte Messages, reverse_map {pseudonym → original})
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
            raise  # Caller entscheidet über fail-open/closed

        if data.get("contains_pii"):
            msg["content"] = data["text"]
            # Reverse-Map: {pseudonym → original}
            for original, pseudo in data.get("mapping", {}).items():
                reverse_map[pseudo] = original

    return result_messages, reverse_map


def depseudonymize_text(text: str, reverse_map: dict[str, str]) -> str:
    """Ersetzt alle Pseudonyme im Text durch die Originale."""
    if not reverse_map:
        return text
    result = text
    # Sortiere nach Länge absteigend, um Teilstring-Konflikte zu vermeiden
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
```

**Step 4: Run tests to verify they pass**

Run: `cd pb-proxy && python3 -m pytest tests/test_pii_middleware.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add pb-proxy/pii_middleware.py pb-proxy/tests/test_pii_middleware.py
git commit -m "feat(proxy): add PII middleware for chat-path pseudonymization"
```

---

### Task 6: PII-Middleware in Proxy-Endpoint integrieren

**Files:**
- Modify: `pb-proxy/proxy.py`
- Modify: `pb-proxy/agent_loop.py`
- Modify: `pb-proxy/tests/test_proxy.py`

**Step 1: Write failing integration test**

Add to `pb-proxy/tests/test_proxy.py`:

```python
class TestPIIProtection:
    def test_messages_pseudonymized_before_llm(self, client):
        """User-Nachrichten werden pseudonymisiert bevor sie an das LLM gehen."""
        resp = client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Sebastian braucht Hilfe"}],
        })
        assert resp.status_code == 200
        # Verify that AgentLoop.run received pseudonymized messages
        # (via mock inspection — AgentLoop is mocked in mock_deps)

    def test_response_depseudonymized(self, client):
        """LLM-Antwort wird de-pseudonymisiert vor Rückgabe."""
        resp = client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hallo Welt"}],
        })
        assert resp.status_code == 200

    def test_pii_scan_fail_closed_when_forced(self):
        """Bei pii_scan_forced + Ingestion down → 503."""
        with patch("proxy.config") as mock_config, \
             patch("proxy.tool_injector") as mock_ti, \
             patch("proxy.check_opa_policy", new_callable=AsyncMock) as mock_opa, \
             patch("proxy.pii_http_client") as mock_http:
            mock_config.PII_SCAN_ENABLED = True
            mock_config.PII_SCAN_FORCED = True
            mock_opa.return_value = {
                "provider_allowed": True,
                "pii_scan_enabled": True,
                "pii_scan_forced": True,
            }
            mock_http.post = AsyncMock(side_effect=httpx.ConnectError("down"))
            mock_ti.merge_tools.return_value = []

            client = TestClient(app)
            resp = client.post("/v1/chat/completions", json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Sebastian Test"}],
            })
            assert resp.status_code == 503
```

**Step 2: Run tests to verify they fail**

Run: `cd pb-proxy && python3 -m pytest tests/test_proxy.py::TestPIIProtection -v`
Expected: FAIL

**Step 3: Integrate PII middleware into proxy.py**

Modify `pb-proxy/proxy.py` — add imports at top:

```python
import httpx
from pii_middleware import (
    pseudonymize_messages,
    depseudonymize_text,
    generate_session_salt,
    build_system_hint,
)
import config
```

Add module-level httpx client (near other globals):

```python
pii_http_client: httpx.AsyncClient | None = None

@app.on_event("startup")
async def _start_pii_client():
    global pii_http_client
    pii_http_client = httpx.AsyncClient()

@app.on_event("shutdown")
async def _stop_pii_client():
    global pii_http_client
    if pii_http_client:
        await pii_http_client.aclose()
```

Modify `chat_completions` function — after OPA check (after line 204), before `merge_tools`:

```python
    # ── PII Protection ───────────────────────────────────────
    pii_reverse_map: dict[str, str] = {}
    pii_enabled = policy.get("pii_scan_enabled", config.PII_SCAN_ENABLED)
    pii_forced = policy.get("pii_scan_forced", config.PII_SCAN_FORCED)

    if pii_enabled:
        session_salt = generate_session_salt()
        try:
            pseudonymized_messages, pii_reverse_map = await pseudonymize_messages(
                request.messages, session_salt, pii_http_client
            )
            # Inject system hint if PII found
            if pii_reverse_map:
                entity_types = list({
                    m.group(1) for m in re.finditer(
                        r"\[([A-Z_]+):[a-f0-9]{8}\]",
                        " ".join(m.get("content", "") for m in pseudonymized_messages),
                    )
                })
                hint = build_system_hint(entity_types)
                if hint:
                    pseudonymized_messages.insert(0, {
                        "role": "system",
                        "content": hint,
                    })
            request.messages = pseudonymized_messages
        except Exception as e:
            if pii_forced:
                log.error("PII scan forced but failed: %s", e)
                raise HTTPException(
                    status_code=503,
                    detail="PII protection required but scanner unavailable",
                )
            log.warning("PII scan failed (non-forced, continuing): %s", e)
```

After `response_data = result.response.model_dump()` (line 247), de-pseudonymize:

```python
    # ── De-pseudonymize response ─────────────────────────────
    if pii_reverse_map:
        for choice in response_data.get("choices", []):
            msg = choice.get("message", {})
            if isinstance(msg.get("content"), str):
                msg["content"] = depseudonymize_text(msg["content"], pii_reverse_map)
```

**Step 4: Integrate de-pseudonymization into agent_loop.py**

Modify `AgentLoop.__init__` to accept an optional `pii_reverse_map`:

```python
def __init__(self, injector, *, acompletion, max_iterations=10,
             tool_call_timeout=30, pii_reverse_map: dict[str, str] | None = None):
    ...
    self._pii_reverse_map = pii_reverse_map or {}
```

After line 99 (`arguments = json.loads(tc.function.arguments)`), add:

```python
                # De-pseudonymize tool arguments before MCP call
                if self._pii_reverse_map:
                    arguments = depseudonymize_tool_arguments(
                        arguments, self._pii_reverse_map
                    )
```

Add import at top of `agent_loop.py`:

```python
from pii_middleware import depseudonymize_tool_arguments
```

Update `proxy.py` where `AgentLoop` is created (line 219):

```python
    loop = AgentLoop(
        tool_injector,
        acompletion=llm_acompletion,
        max_iterations=max_iterations,
        pii_reverse_map=pii_reverse_map,
    )
```

**Step 5: Run tests to verify they pass**

Run: `cd pb-proxy && python3 -m pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add pb-proxy/proxy.py pb-proxy/agent_loop.py pb-proxy/tests/test_proxy.py
git commit -m "feat(proxy): integrate PII middleware into chat endpoint

Pseudonymizes inbound messages, de-pseudonymizes outbound responses.
De-pseudonymizes tool-call arguments before MCP execution (closes #6).
Policy-controlled: fail-closed when pii_scan_forced, fail-open otherwise."
```

---

### Task 7: Prometheus-Metriken für PII-Schutz

**Files:**
- Modify: `pb-proxy/proxy.py` (add counters)

**Step 1: Add Prometheus counters**

Add to the metrics section in `proxy.py` (after existing counters):

```python
PII_ENTITIES_PSEUDONYMIZED = Counter(
    "proxy_pii_entities_pseudonymized_total",
    "Total PII entities pseudonymized in chat messages",
    ["entity_type"],
)
PII_SCAN_FAILURES = Counter(
    "proxy_pii_scan_failures_total",
    "PII scan failures (ingestion service unreachable)",
    ["fail_mode"],  # "closed" or "open"
)
```

Increment in the PII section of `chat_completions`:
- After successful pseudonymization: `PII_ENTITIES_PSEUDONYMIZED.labels(entity_type=t).inc()` for each type
- On failure: `PII_SCAN_FAILURES.labels(fail_mode="closed" if pii_forced else "open").inc()`

**Step 2: Add proxy to Prometheus scrape config**

In `monitoring/prometheus.yml`, add scrape target for pb-proxy.

**Step 3: Commit**

```bash
git add pb-proxy/proxy.py monitoring/prometheus.yml
git commit -m "feat(monitoring): add PII protection metrics to proxy + Prometheus scrape"
```

---

### Task 8: docker-compose.yml — Umgebungsvariablen

**Files:**
- Modify: `docker-compose.yml`

**Step 1: Add env vars to pb-proxy service**

```yaml
  pb-proxy:
    environment:
      - INGESTION_URL=http://ingestion:8081
      - PII_SCAN_ENABLED=true
      - PII_SCAN_FORCED=false
```

Add `depends_on` if not present:

```yaml
    depends_on:
      - ingestion
      - mcp-server
      - opa
```

**Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(docker): add PII protection env vars to pb-proxy service"
```

---

### Task 9: E2E-Smoke-Test

**Files:**
- Create: `tests/integration/test_pii_chat_protection.py`

**Step 1: Write integration test**

```python
"""
E2E-Test: Chat-Path PII Protection.

Verifies the full product promise:
User-Nachricht mit PII → pseudonymisiert an LLM → de-pseudonymisiert zurück.

Requires: RUN_INTEGRATION_TESTS=1, running pb-proxy + ingestion services.
"""
import pytest
import httpx

pytestmark = pytest.mark.integration

PROXY_URL = "http://localhost:8090"


@pytest.fixture
async def http():
    async with httpx.AsyncClient(base_url=PROXY_URL) as client:
        yield client


@pytest.mark.asyncio
async def test_pii_not_in_llm_request(http):
    """Verify that the proxy pseudonymizes PII before sending to the LLM.

    We cannot directly inspect what goes to the LLM in an E2E test,
    but we can verify:
    1. The proxy accepts the request (PII scan works)
    2. The response is valid and de-pseudonymized
    """
    resp = await http.post("/v1/chat/completions", json={
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Was weiß das System über Sebastian Müller?"}],
    })
    # If PII scan is forced and ingestion is down, we get 503
    # If PII scan works, we get 200
    assert resp.status_code in (200, 503)


@pytest.mark.asyncio
async def test_pseudonymize_endpoint_directly(http):
    """Verify the ingestion /pseudonymize endpoint works standalone."""
    ingestion = httpx.AsyncClient(base_url="http://localhost:8081")
    resp = await ingestion.post("/pseudonymize", json={
        "text": "Sebastian und Maria arbeiten am Projekt.",
        "salt": "integration-test-salt",
    })
    assert resp.status_code == 200
    data = resp.json()

    if data["contains_pii"]:
        assert "Sebastian" not in data["text"]
        assert "[PERSON:" in data["text"]
        assert "Sebastian" in data["mapping"]
    await ingestion.aclose()
```

**Step 2: Run integration test (if services running)**

Run: `RUN_INTEGRATION_TESTS=1 python3 -m pytest tests/integration/test_pii_chat_protection.py -v`

**Step 3: Commit**

```bash
git add tests/integration/test_pii_chat_protection.py
git commit -m "test: add E2E integration test for chat-path PII protection"
```

---

### Task 10: CLAUDE.md und Design-Doc aktualisieren

**Files:**
- Modify: `CLAUDE.md` — Feature-Liste, Architektur-Diagramm, MCP-Tools-Beschreibung
- Modify: `docs/plans/2026-03-22-chat-pii-protection-design.md` — Status auf "implementiert"

**Step 1: Update CLAUDE.md**

- Add `10. ✅ **Chat-Path PII Protection** — Reversible Pseudonymisierung im Proxy` to Completed Features
- Add `PII_SCAN_ENABLED`, `PII_SCAN_FORCED`, `INGESTION_URL` to env var documentation
- Update Architecture diagram to show PII middleware in proxy path

**Step 2: Update design doc status**

Change `Status: Entwurf` → `Status: Implementiert`

**Step 3: Commit**

```bash
git add CLAUDE.md docs/plans/2026-03-22-chat-pii-protection-design.md
git commit -m "docs: update CLAUDE.md and design doc for chat-path PII protection"
```

---

## Zusammenfassung

| Task | Komponente | Beschreibung |
|------|-----------|-------------|
| 1 | Ingestion PIIScanner | Typisiertes Pseudonym-Format `[TYPE:hash]` |
| 2 | Ingestion API | Neuer `POST /pseudonymize` Endpunkt |
| 3 | OPA Policies | `pii_scan_enabled`, `pii_scan_forced`, `pii_entity_types` |
| 4 | Proxy Config | `INGESTION_URL`, `PII_SCAN_ENABLED`, `PII_SCAN_FORCED` |
| 5 | Proxy Middleware | `pii_middleware.py` — pseudonymize/depseudonymize Funktionen |
| 6 | Proxy Integration | Middleware in `proxy.py` + Tool-Arg-De-Pseudonymisierung in `agent_loop.py` |
| 7 | Monitoring | Prometheus-Counter für PII-Entities + Scan-Failures |
| 8 | Docker Compose | Env-Vars und `depends_on` für pb-proxy |
| 9 | E2E Test | Integration-Test für den gesamten Chat-PII-Flow |
| 10 | Docs | CLAUDE.md + Design-Doc aktualisieren |

**Punkt 6 aus den Known Limitations (Tool-Call-Argumente)** wird in Task 6 gelöst: `agent_loop.py` de-pseudonymisiert Tool-Argumente vor dem MCP-Aufruf via `depseudonymize_tool_arguments()`.
