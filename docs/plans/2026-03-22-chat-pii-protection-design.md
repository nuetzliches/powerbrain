# Chat-Path PII Protection — Design

**Date:** 2026-03-22
**Status:** Implemented
**Approach:** A — Proxy middleware calls ingestion service

## Problem

Powerbrain pseudonymizes PII in the knowledge base (ingestion pipeline), but **not in the chat path**. User messages sent through the `pb-proxy` to LLM providers go out in plaintext. Personal names, email addresses and other PII reach the external LLM provider unprotected.

## Solution

Reversible PII pseudonymization as middleware in the `pb-proxy`:

1. **Inbound:** User messages are pseudonymized before the LLM call
2. **LLM** only sees typed pseudonyms (`[PERSON:a1b2c3d4]`)
3. **Outbound:** LLM response is de-pseudonymized before being returned to the user
4. **Mapping** lives ephemerally in the request scope (in-memory, no persistence)
5. **OPA policy** controls activation, enforcement and entity types

## Data flow

```
User: "Sebastian und Maria brauchen Zugriff auf Projekt Alpha"
        │
        ▼  pb-proxy PII middleware (inbound)
        │
        ├─ OPA: kb.proxy.pii_scan_enabled? → yes
        ├─ HTTP POST ingestion:8081/pseudonymize
        │    Request:  {"text": "Sebastian und Maria brauchen...", "salt": "<session-salt>"}
        │    Response: {
        │      "text": "[PERSON:a1b2c3d4] und [PERSON:e5f6g7h8] brauchen Zugriff auf Projekt Alpha",
        │      "mapping": {"Sebastian": "a1b2c3d4", "Maria": "e5f6g7h8"},
        │      "entities": [{"type":"PERSON","start":0,"end":9,"score":0.95}, ...]
        │    }
        ├─ Store reverse_map in the request scope:
        │    {"[PERSON:a1b2c3d4]": "Sebastian", "[PERSON:e5f6g7h8]": "Maria"}
        ├─ Replace the original text in ALL user messages
        ├─ Inject system prompt hint (only when PII was detected)
        ▼
   LLM sees: "[PERSON:a1b2c3d4] und [PERSON:e5f6g7h8] brauchen Zugriff auf Projekt Alpha"
        │
        ▼  LLM replies
        │
   LLM response: "[PERSON:a1b2c3d4] sollte Admin-Rechte für Projekt Alpha bekommen."
        │
        ▼  pb-proxy PII middleware (outbound)
        │
        ├─ String-replace all pseudonyms from reverse_map
        ▼
   User sees: "Sebastian sollte Admin-Rechte für Projekt Alpha bekommen."
```

## Pseudonym format

**Typed:** `[TYPE:8-char-hex]`

Examples:
- `[PERSON:a1b2c3d4]`
- `[EMAIL:f9e8d7c6]`
- `[PHONE:1a2b3c4d]`
- `[IBAN:5e6f7a8b]`

Advantages over bare hex:
- LLM recognizes the entity type (name, email, etc.)
- Easy to identify via regex: `\[([A-Z_]+):([a-f0-9]{8})\]`
- Unique — does not collide with natural text

**Salt:** Randomly generated per request/session (not the project salt from the knowledge base). Prevents correlation between chat pseudonyms and stored data.

## System prompt injection

Only when PII was detected, a hint is prepended as a system message:

```
Die folgende Konversation enthält typisierte Pseudonyme (z.B. [PERSON:a1b2c3d4]).
Behandle sie als normale Namen bzw. Werte. Versuche nicht, die Originale zu erraten.
```

## OPA policy

Extension of `opa-policies/kb/proxy.rego`:

```rego
package kb.proxy

# --- PII scan in chat path ---

# Default: active
default pii_scan_enabled = true

# Policy can force the scan — no opt-out possible
default pii_scan_forced = false

# Opt-out only when: admin + explicitly requested + not forced
pii_scan_opt_out_allowed {
    input.agent_role == "admin"
    input.pii_scan_opt_out == true
    not pii_scan_forced
}

# Scan disabled only on allowed opt-out
pii_scan_enabled = false {
    pii_scan_opt_out_allowed
}

# Which entity types are pseudonymized
pii_entity_types := ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "LOCATION"]

# System prompt injection allowed
default pii_system_prompt_injection = true
```

## Fail behavior (policy-driven)

| `pii_scan_forced` | Ingestion down | Behavior |
|---|---|---|
| `true` | down | **HTTP 503** — Request blocked |
| `false` | down | Fail-open with warning in log |

Those who force the scan accept the availability risk. Those who use it optionally get graceful degradation.

## Changes per component

### A. Ingestion service — new endpoint `POST /pseudonymize`

Pure scan + pseudonymization, no storage:

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

- Uses existing `PIIScanner.pseudonymize_text()`
- Change: typed pseudonym format `[TYPE:hash]` instead of bare hex
- No vault write, no embedding, no Qdrant

### B. pb-proxy — new middleware `pii_middleware.py`

```
pb-proxy/
  ├─ pii_middleware.py (NEW)
  │    ├─ pseudonymize_messages(messages, session_salt) → (messages, reverse_map)
  │    ├─ depseudonymize_response(response, reverse_map) → response
  │    └─ build_system_hint(entity_types) → str
  ├─ proxy.py (updated)
  │    ├─ before AgentLoop: OPA check → pseudonymize_messages()
  │    ├─ after AgentLoop: depseudonymize_response()
  │    └─ Prometheus counter: pii_entities_pseudonymized_total
```

### C. OPA policies

`opa-policies/kb/proxy.rego` extended with PII rules (see above).
New Rego tests in `opa-policies/kb/test_proxy_pii.rego`.

### D. No changes to

- MCP server (already has PII scanning on queries in the audit log)
- Qdrant / PostgreSQL / Sealed Vault (chat mapping is ephemeral)
- Reranker

## Known limitations

1. ~~**Text only**~~ **Resolved:** Non-text content (images, PDFs, files) is controlled via OPA policy: `block` (reject), `placeholder` (replace with hint), `allow` (let through with warning). Default: `placeholder`. PII scanning still applies only to text — but non-text content cannot slip through unnoticed.

2. **LLM can corrupt pseudonyms** — if the LLM fragments `[PERSON:a1b2c3d4]`, rephrases it or splits it into substrings, reverse mapping fails. Mitigation: system prompt hint and robust regex matching.

3. **No audit trail** for chat PII — deliberate design decision in favor of data minimization. Chat contents (neither original nor pseudonymized) are not persisted.

4. **Streaming responses** — Pseudonym replacement in streamed chunks requires buffering, since pseudonyms may span chunk boundaries (`[PERSON:a1b2` | `c3d4]`). First step: only support non-streaming responses.

5. **Presidio detection rate** — Presidio does not reliably detect all PII (e.g. unusual names, abbreviations, context-dependent PII). False negatives are possible.

6. ~~**Tool call arguments**~~ **Resolved:** Proxy de-pseudonymizes tool call arguments before MCP calls via `depseudonymize_tool_arguments()`.
