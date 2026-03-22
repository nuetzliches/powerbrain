# Chat-Path PII Protection — Design

**Datum:** 2026-03-22
**Status:** Implementiert
**Ansatz:** A — Proxy-Middleware ruft Ingestion-Service

## Problem

Powerbrain pseudonymisiert PII in der Wissensdatenbank (Ingestion-Pipeline), aber **nicht im Chat-Pfad**. User-Nachrichten, die über den `pb-proxy` an LLM-Provider gesendet werden, gehen im Klartext raus. Personennamen, E-Mail-Adressen und andere PII erreichen den externen LLM-Provider ungeschützt.

## Lösung

Reversible PII-Pseudonymisierung als Middleware im `pb-proxy`:

1. **Inbound:** User-Nachrichten werden vor dem LLM-Aufruf pseudonymisiert
2. **LLM** sieht nur typisierte Pseudonyme (`[PERSON:a1b2c3d4]`)
3. **Outbound:** LLM-Antwort wird vor der Rückgabe an den User de-pseudonymisiert
4. **Mapping** lebt ephemeral im Request-Scope (In-Memory, keine Persistenz)
5. **OPA-Policy** steuert Aktivierung, Erzwingung und Entity-Typen

## Datenfluss

```
User: "Sebastian und Maria brauchen Zugriff auf Projekt Alpha"
        │
        ▼  pb-proxy PII-Middleware (inbound)
        │
        ├─ OPA: kb.proxy.pii_scan_enabled? → ja
        ├─ HTTP POST ingestion:8081/pseudonymize
        │    Request:  {"text": "Sebastian und Maria brauchen...", "salt": "<session-salt>"}
        │    Response: {
        │      "text": "[PERSON:a1b2c3d4] und [PERSON:e5f6g7h8] brauchen Zugriff auf Projekt Alpha",
        │      "mapping": {"Sebastian": "a1b2c3d4", "Maria": "e5f6g7h8"},
        │      "entities": [{"type":"PERSON","start":0,"end":9,"score":0.95}, ...]
        │    }
        ├─ Speichere reverse_map im Request-Scope:
        │    {"[PERSON:a1b2c3d4]": "Sebastian", "[PERSON:e5f6g7h8]": "Maria"}
        ├─ Ersetze in ALLEN user-Messages den Originaltext
        ├─ Injiziere System-Prompt-Hinweis (nur wenn PII erkannt)
        ▼
   LLM sieht: "[PERSON:a1b2c3d4] und [PERSON:e5f6g7h8] brauchen Zugriff auf Projekt Alpha"
        │
        ▼  LLM antwortet
        │
   LLM-Response: "[PERSON:a1b2c3d4] sollte Admin-Rechte für Projekt Alpha bekommen."
        │
        ▼  pb-proxy PII-Middleware (outbound)
        │
        ├─ String-Replace aller Pseudonyme aus reverse_map
        ▼
   User sieht: "Sebastian sollte Admin-Rechte für Projekt Alpha bekommen."
```

## Pseudonym-Format

**Typisiert:** `[TYPE:8-char-hex]`

Beispiele:
- `[PERSON:a1b2c3d4]`
- `[EMAIL:f9e8d7c6]`
- `[PHONE:1a2b3c4d]`
- `[IBAN:5e6f7a8b]`

Vorteile gegenüber nacktem Hex:
- LLM erkennt den Entity-Typ (Name, E-Mail, etc.)
- Einfach per Regex identifizierbar: `\[([A-Z_]+):([a-f0-9]{8})\]`
- Eindeutig — kollidiert nicht mit natürlichem Text

**Salt:** Zufällig pro Request/Session generiert (nicht der Projekt-Salt aus der Wissensdatenbank). Verhindert Korrelation zwischen Chat-Pseudonymen und gespeicherten Daten.

## System-Prompt-Injection

Nur wenn PII erkannt wurde, wird ein Hinweis als System-Message vorangestellt:

```
Die folgende Konversation enthält typisierte Pseudonyme (z.B. [PERSON:a1b2c3d4]).
Behandle sie als normale Namen bzw. Werte. Versuche nicht, die Originale zu erraten.
```

## OPA-Policy

Erweiterung von `opa-policies/kb/proxy.rego`:

```rego
package kb.proxy

# --- PII-Scan im Chat-Pfad ---

# Default: aktiv
default pii_scan_enabled = true

# Policy kann Scan erzwingen — kein Opt-out möglich
default pii_scan_forced = false

# Opt-out nur wenn: Admin + explizit angefragt + nicht forced
pii_scan_opt_out_allowed {
    input.agent_role == "admin"
    input.pii_scan_opt_out == true
    not pii_scan_forced
}

# Scan deaktiviert nur bei erlaubtem Opt-out
pii_scan_enabled = false {
    pii_scan_opt_out_allowed
}

# Welche Entity-Typen pseudonymisiert werden
pii_entity_types := ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "LOCATION"]

# System-Prompt-Injection erlaubt
default pii_system_prompt_injection = true
```

## Fail-Verhalten (policy-gesteuert)

| `pii_scan_forced` | Ingestion down | Verhalten |
|---|---|---|
| `true` | down | **HTTP 503** — Request blockiert |
| `false` | down | Fail-open mit Warning ins Log |

Wer den Scan erzwingt, akzeptiert das Verfügbarkeitsrisiko. Wer ihn optional nutzt, bekommt Graceful Degradation.

## Änderungen pro Komponente

### A. Ingestion-Service — neuer Endpunkt `POST /pseudonymize`

Reiner Scan+Pseudonymisierung, kein Speichern:

```python
# Request
{"text": "Sebastian braucht Hilfe", "salt": "random-session-salt-xyz"}

# Response
{
  "text": "[PERSON:a1b2c3d4] braucht Hilfe",
  "mapping": {"Sebastian": "a1b2c3d4"},
  "entities": [{"type": "PERSON", "start": 0, "end": 9, "score": 0.95}]
}
```

- Nutzt bestehenden `PIIScanner.pseudonymize_text()`
- Anpassung: typisiertes Pseudonym-Format `[TYPE:hash]` statt nacktem Hex
- Kein Vault-Write, kein Embedding, kein Qdrant

### B. pb-proxy — neue Middleware `pii_middleware.py`

```
pb-proxy/
  ├─ pii_middleware.py (NEU)
  │    ├─ pseudonymize_messages(messages, session_salt) → (messages, reverse_map)
  │    ├─ depseudonymize_response(response, reverse_map) → response
  │    └─ build_system_hint(entity_types) → str
  ├─ proxy.py (angepasst)
  │    ├─ vor AgentLoop: OPA-Check → pseudonymize_messages()
  │    ├─ nach AgentLoop: depseudonymize_response()
  │    └─ Prometheus-Counter: pii_entities_pseudonymized_total
```

### C. OPA-Policies

`opa-policies/kb/proxy.rego` erweitert um PII-Regeln (siehe oben).
Neue Rego-Tests in `opa-policies/kb/test_proxy_pii.rego`.

### D. Keine Änderungen an

- MCP-Server (hat bereits PII-Scan auf Queries im Audit-Log)
- Qdrant / PostgreSQL / Sealed Vault (Chat-Mapping ist ephemeral)
- Reranker

## Known Limitations

1. ~~**Nur Text**~~ **Gelöst:** Non-text Content (Bilder, PDFs, Dateien) wird per OPA-Policy gesteuert: `block` (ablehnen), `placeholder` (ersetzen durch Hinweis), `allow` (durchlassen mit Warning). Default: `placeholder`. PII-Scanning erfolgt weiterhin nur für Text — aber non-text Content kann nicht unbemerkt durchrutschen.

2. **LLM kann Pseudonyme verfälschen** — wenn das LLM `[PERSON:a1b2c3d4]` fragmentiert, umformuliert oder in Teilstrings aufteilt, schlägt das Reverse-Mapping fehl. Mitigation: System-Prompt-Hinweis und robustes Regex-Matching.

3. **Kein Audit-Trail** für Chat-PII — bewusste Designentscheidung zugunsten Datensparsamkeit. Chat-Inhalte (weder Original noch pseudonymisiert) werden nicht persistiert.

4. **Streaming-Responses** — Pseudonym-Replacement in gestreamten Chunks erfordert Buffering, da Pseudonyme über Chunk-Grenzen gehen können (`[PERSON:a1b2` | `c3d4]`). Erster Schritt: nur Non-Streaming-Responses unterstützen.

5. **Presidio-Erkennungsrate** — Presidio erkennt nicht alle PII zuverlässig (z.B. ungewöhnliche Namen, Abkürzungen, Kontext-abhängige PII). False Negatives sind möglich.

6. ~~**Tool-Call-Argumente**~~ **Gelöst:** Proxy de-pseudonymisiert Tool-Call-Argumente vor MCP-Aufrufen via `depseudonymize_tool_arguments()`.
