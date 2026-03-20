# Audit-Log PII Protection

**Date:** 2026-03-21
**Status:** Approved
**Scope:** PII-Hygiene + Zugriffskontrolle + Retention für `agent_access_log`

## Problem

The `search_knowledge` tool stores raw query text in `agent_access_log.request_context`.
If an agent searches for "Vertrag von Max Mustermann", that name is persisted verbatim.
This creates three DSGVO issues:

1. **Art. 5(1)(c) Datenminimierung** — Full query text is stored when a masked version would suffice for audit purposes
2. **Art. 17 Recht auf Löschung** — No mechanism to find/anonymize PII in audit log query text
3. **Broken anonymization** — `retention_cleanup.py` checks `contains_pii = true`, but `log_access()` never sets this column

Additional gaps:
- `agent_access_log` has no RLS — anyone with DB access can read all queries
- `get_code_context` does not log its query at all (inconsistent with `search_knowledge`)
- No time-based retention policy for audit logs

## Design: Three Pillars

### Pillar 1: PII Hygiene — Scan Query Text Before Logging

**Mechanism:** The ingestion service gets a new `POST /scan` endpoint exposing the existing `PIIScanner`.
The MCP server calls this endpoint before writing audit log entries that contain query text.

**Flow:**

```
Agent → MCP-Server → search_knowledge()
                          │
                          ├─ Search results (Qdrant + OPA + Rerank, as before)
                          │
                          ├─ POST http://ingestion:8081/scan
                          │    Request:  {"text": "<query>", "language": "de"}
                          │    Response: {"contains_pii": true,
                          │               "masked_text": "Vertrag von <PERSON>",
                          │               "entity_types": ["PERSON"]}
                          │
                          └─ log_access(..., context={
                                "query": masked_text,           // masked, not original
                                "query_contains_pii": true,
                                "pii_entity_types": ["PERSON"],
                                "qdrant_results": N,
                                "after_policy": N,
                                "after_rerank": N,
                                "vault_access_requested": bool
                             }, contains_pii=true)
```

**No fallback.** If `/scan` is unreachable, `log_access()` raises an error and the
entire request fails. Rationale: data minimization is not optional.

**Consistency fix:** `get_code_context` will also log its query text (masked) — same
behavior as `search_knowledge`.

**Scanner API contract:**

```
POST /scan
Content-Type: application/json

Request:
{
  "text": "string",          // required
  "language": "de"           // optional, default "de"
}

Response:
{
  "contains_pii": bool,
  "masked_text": "string",   // text with PII replaced by <TYPE> placeholders
  "entity_types": ["PERSON", "EMAIL_ADDRESS", ...]
}
```

### Pillar 2: Access Control — Secure Audit Logs at DB Level

**Changes:**

1. New SQL migration enabling RLS on `agent_access_log`
2. `mcp_app` role: INSERT only (no SELECT, UPDATE, DELETE)
3. New `mcp_auditor` role: SELECT only (for monitoring/compliance)
4. No MCP tool exposes audit logs — they are only accessible via direct DB connection

**Policy:**

```sql
ALTER TABLE agent_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_access_log FORCE ROW LEVEL SECURITY;

-- App can only insert
CREATE POLICY audit_insert_only ON agent_access_log
  FOR INSERT TO mcp_app WITH CHECK (true);

-- Auditor can only read
CREATE POLICY audit_read_only ON agent_access_log
  FOR SELECT TO mcp_auditor USING (true);
```

### Pillar 3: Retention — Time-Based Anonymization

**Changes:**

1. New function `anonymize_old_audit_logs()` in `retention_cleanup.py`
2. Anonymizes `request_context` after configurable N days (default: 365, via env var `AUDIT_RETENTION_DAYS`)
3. Sets `request_context = '{"anonymized": true}'::jsonb` for all entries older than retention period
4. Existing anonymization now works correctly because `contains_pii` is set by Pillar 1

**SQL:**

```sql
UPDATE agent_access_log
SET request_context = '{"anonymized": true}'::jsonb
WHERE created_at < now() - interval '1 day' * $1
  AND request_context != '{"anonymized": true}'::jsonb;
```

## Decision: Why Not a New OPA Classification?

The existing classification system (`public`/`internal`/`confidential`/`restricted`) protects
**document content** — it controls which agent can see which documents. Adding a `private`
level for audit logs would conflate two different concerns:

- **Classification** answers: "Who may read this document?"
- **PII hygiene** answers: "Does this operational data contain personal information?"

Audit logs are operational data, not documents. They need access control (Pillar 2) and
data minimization (Pillar 1), but not document-level classification. The correct boundary
is: classify documents, sanitize operational data.

## Files to Change

| File | Change |
|---|---|
| `ingestion/ingestion_api.py` | New `POST /scan` endpoint |
| `mcp-server/server.py` | `log_access()` calls `/scan`, sets `contains_pii`, consistent logging for `get_code_context` |
| `init-db/008_audit_rls.sql` | RLS policies on `agent_access_log` |
| `ingestion/retention_cleanup.py` | New `anonymize_old_audit_logs()` function |
| `tests/` | Tests for `/scan` endpoint, `log_access` PII scanning, retention anonymization |
| `docs/bekannte-schwachstellen.md` | Track this as resolved |

## Non-Goals

- PII scanning of `query_data` SQL queries (these are not logged with query text)
- PII scanning of graph queries (not logged with query text)
- Scanning agent_id itself for PII (agent_id is a system identifier, not user input)
- Modularization of other components (separate design)
