# Known Issues & Technical Debt — All Resolved

Documented after code review. Priority levels: P0 (Blocker), P1 (Security-Critical),
P2 (Correctness), P3 (Architecture).

---

## ~~P0 — Blocker: System won't start / Core functionality broken~~ — ALL RESOLVED

### ~~P0-1: MCP Transport: stdio != Docker networking~~ — RESOLVED

**Status:** RESOLVED — MCP server now uses `StreamableHTTPSessionManager`
on port 8080. Prometheus metrics on separate port 9091. Agents in other
Docker containers can call MCP tools via HTTP.

---

### ~~P0-2: `ingestion_api.py` missing~~ — RESOLVED

**Status:** RESOLVED — `ingestion/ingestion_api.py` implemented with endpoints:
`POST /ingest`, `POST /scan`, `POST /snapshots/create`, `GET /health`.

---

### ~~P0-3: `graph_service.py` missing from MCP server image~~ — RESOLVED

**Status:** RESOLVED — `mcp-server/Dockerfile` now copies both files:
`COPY server.py graph_service.py ./`

---

### ~~P0-4: OPA policies not loaded~~ — RESOLVED

**Status:** RESOLVED — OPA container has volume `./opa-policies:/policies:ro`
and loads policies at startup via `/policies` argument.

---

## ~~P1 — Security-Critical~~ — ALL RESOLVED

### ~~P1-1: No authentication — roles are self-declared~~ — RESOLVED

**Status:** RESOLVED — API key authentication implemented. Every agent
requires an `Authorization: Bearer kb_...` header. Keys are stored as SHA-256
hashes in the `api_keys` table and map to a fixed role
(analyst/developer/admin). `agent_id` and `agent_role` are no longer
tool parameters but derived from the verified token.
`AUTH_REQUIRED` env var controls whether authentication is enforced (default: true).

---

### ~~P1-2: SQL injection in `query_data`~~ — RESOLVED

**Status:** RESOLVED — Condition keys in `query_data` are now validated by
`validate_identifier()` against a regex whitelist (`^[a-zA-Z_][a-zA-Z0-9_]*$`)
before being interpolated into SQL strings. Invalid keys return an error.
The `LIMIT` value is also passed as a parameter instead of interpolated.
`validate_identifier` is defined in `graph_service.py` and imported by `server.py`.

---

### ~~P1-3: Cypher injection in `graph_service.py`~~ — RESOLVED

**Status:** RESOLVED — All graph functions (`create_node`, `find_node`,
`delete_node`, `create_relationship`, `find_relationships`, `get_neighbors`,
`find_path`) validate labels, property keys, and relationship types via
`_require_identifier()`, which internally calls `validate_identifier()`.
Only ASCII identifiers (`^[a-zA-Z_][a-zA-Z0-9_]*$`) are accepted.
Invalid inputs raise `ValueError`.

---

## ~~P2 — Correctness / Reliability~~ — ALL RESOLVED

### ~~P2-1: 50 serial OPA calls per search query~~ — RESOLVED

**Status:** RESOLVED — OPA policy checks are now executed in parallel using
`asyncio.gather` instead of serially. Affects `search_knowledge`,
`get_code_context`, and `list_datasets`. Latency drops from N x RTT to ~1 x RTT.
Shared helper function `filter_by_policy` for Qdrant hits.

---

### ~~P2-2: `run_eval.py` bypasses OPA policy filter~~ — RESOLVED

**Status:** RESOLVED — `run_eval.py` now checks every Qdrant result against
OPA `kb/access/allow` before including it in the evaluation. `check_opa_access()`
uses `EVAL_AGENT_ROLE = "analyst"` for consistent access control.
OPA results are cached per classification (only 4 possible values).
On OPA error: fail-closed (access denied).

---

### ~~P2-3: `create_snapshot` endpoint not implemented~~ — RESOLVED

**Status:** RESOLVED — `ingestion_api.py` implements `POST /snapshots/create`,
which calls `snapshot_service.create_snapshot()`. MCP tool `create_snapshot`
delegates correctly to this endpoint.

---

### ~~P2-4: Reference to non-existent `business_rules` table~~ — RESOLVED

**Status:** RESOLVED — `business_rules` was removed from `PG_SNAPSHOT_TABLES` in
`snapshot_service.py`. Business rules are provided exclusively via
OPA policies (`kb.rules`), not via PostgreSQL.

---

### ~~P2-5: PG connection pool lazy-initialized~~ — RESOLVED

**Status:** RESOLVED — PG connection pool is now initialized in a `lifespan`
async context manager with a `SELECT 1` startup healthcheck.
Pool and HTTP client are cleanly closed on shutdown.
`get_pg_pool()` raises `RuntimeError` if pool is not initialized.

---

### ~~P2-6: Apache AGE — known limitations~~ — RESOLVED

**Status:** RESOLVED — Three fixes implemented:
1. Missing `graph_sync_log` migration created (`011_graph_sync_log.sql`)
   with GRANT for `mcp_app` role.
2. AGE agtype parsing hardened: `re.sub` removes `::vertex`, `::edge`,
   `::path`, `::numeric`, and additional agtype suffixes before `json.loads()`.
   Fallback to `{"raw": ...}` on parse error remains in place.
3. `find_path()` uses `shortestPath` with try/except fallback to
   variable-depth MATCH for AGE bugs. Failures are logged.

---

## ~~P3 — Architectural Weaknesses~~ — ALL RESOLVED

### ~~P3-1: No retry / circuit breaker~~ — RESOLVED

**Status:** RESOLVED — `tenacity` retry decorators on critical external calls:
`embed_text` (3 attempts, 2/4/8s backoff for Ollama), `check_opa_policy`
(2 attempts, 0.5/1s backoff). Retryable exceptions (ConnectError, TimeoutException)
are re-raised for tenacity; other errors are handled immediately.
`log_access` PII scan has try/except with 1 retry on connection error.
Qdrant client with `timeout=30`. Reranker retains existing graceful fallback.

---

### ~~P3-2: No rate limiting~~ — RESOLVED

**Status:** RESOLVED — In-memory token bucket rate limiting implemented.
Per-agent throttling based on `agent_id` from auth context.
Configurable via env vars (`RATE_LIMIT_RPM`, `RATE_LIMIT_BURST`,
`RATE_LIMIT_ENABLED`). Prometheus counter `kb_rate_limit_rejected_total`
for monitoring. On limit exceeded: HTTP 429 with Retry-After header.

---

### ~~P3-3: Ingestion pipeline is a stub~~ — RESOLVED

**Status:** RESOLVED — `ingest_data` MCP tool cleaned up: schema reduced to
`text`+`url` (CSV/JSON/git_repo removed). New `/ingest/chunks` adapter endpoint
in the ingestion API for pre-chunked data. Source tracking improved with
`source_type:inline` format. `DEFAULT_COLLECTION` constant instead of magic string.

---

### ~~P3-4: Monitoring port conflict (MCP server)~~ — RESOLVED

**Status:** RESOLVED — Resolved together with P0-1. MCP endpoint on port 8080,
Prometheus metrics on port 9091.

---

## Sprint Prioritization

| Priority | Issues | Status |
|----------|--------|--------|
| ~~Sprint 1 (Blockers)~~ | ~~P0-1, P0-2, P0-3, P0-4~~ | ~~resolved~~ |
| ~~Sprint 2 (Security)~~ | ~~P1-1, P1-2, P1-3~~ | ~~resolved~~ |
| ~~Sprint 3 (Correctness)~~ | ~~P2-1, P2-3, P2-5~~ | ~~resolved~~ |
| ~~Sprint 4 (Correctness + Resilience)~~ | ~~P2-2, P2-4, P3-1~~ | ~~resolved~~ |
| ~~Sprint 5 (AGE Hardening + Rate Limit + Ingestion)~~ | ~~P2-6, P3-2, P3-3~~ | ~~resolved~~ |

**All known issues are resolved.** The system is suitable for **internal testing** after Sprints 1-5.
For **production use**, additionally requires TLS + secrets management.

---

## ~~Phase 2 after Search-first MVP~~ — ALL RESOLVED

The initial MVP iteration focused on the working search path.
The following topics were prioritized as phase 2 work and are now resolved:

- ~~SQL and Cypher hardening outside the MVP search path~~ (P1-2, P1-3)
- ~~Complete ingestion API~~ (P3-3)
- ~~Snapshot and evaluation side paths~~ (P2-3)

---

## Resolved — Sealed Vault (Dual Storage)

The following issues were resolved as part of the sealed vault implementation:

### RESOLVED: OPA `kb.privacy.pii_action` never called

**Status:** RESOLVED — The ingestion pipeline now calls `kb.privacy.pii_action`
to determine the correct action (pseudonymize, redact, block) for PII-containing data.
The dual_storage path uses the result to control vault storage.

### RESOLVED: `pseudonymize_text()` never called

**Status:** RESOLVED — `pseudonymize_text()` is now called in the dual storage path
of the ingestion pipeline to deterministically pseudonymize PII texts
before storing them in Qdrant. Originals go into the sealed vault.

### RESOLVED: `pii_scan_log` never written

**Status:** RESOLVED — The ingestion pipeline now writes an entry to `pii_scan_log`
for every PII scan, including scan result, detected entity types,
and the chosen action (pseudonymize/redact/block).

### RESOLVED: `fields_to_redact` never applied

**Status:** RESOLVED — The MCP server applies `vault_fields_to_redact` from the
OPA policy when retrieving vault originals. Fields are redacted by
purpose, so only purpose-bound information is visible.

### RESOLVED: Bug in `pseudonymize_text()` — same pseudonym for all entities of the same type

**Status:** RESOLVED — The bug where all entities of the same type (e.g., multiple
PERSON entities) received the same pseudonym value has been fixed. The function
now uses the entity text as part of the HMAC input, so each entity receives a
unique but deterministic pseudonym value.

### RESOLVED: `data_subjects`, `datasets.pseudonymized` never populated

**Status:** RESOLVED — The vault path now populates the relevant fields:
`data_subjects` are linked with detected subject references during PII ingestion,
and the pseudonymization status is correctly tracked.

---

## Resolved — Audit Log PII Protection

The following gaps in the audit log were resolved:

### RESOLVED: PII stored in audit log query text

**Status:** RESOLVED — `log_access()` calls the `/scan` endpoint of the ingestion
service before storing. Query texts are replaced with masked versions
(`"Max Mustermann"` -> `"<PERSON>"`). `contains_pii` is set correctly.
`get_code_context` now also consistently logs query text (masked).

### RESOLVED: `contains_pii` never set — anonymization ineffective

**Status:** RESOLVED — `log_access()` sets `contains_pii` based on the PII scan
result. The existing anonymization in `retention_cleanup.py` thus works correctly
for dataset-specific deletions.

### RESOLVED: No access control on audit logs

**Status:** RESOLVED — Migration `008_audit_rls.sql` enables Row-Level Security
(with FORCE) on `agent_access_log`. `mcp_app` can only INSERT, new role
`mcp_auditor` can only SELECT. No MCP tool exposes the logs.

### RESOLVED: No time-based audit log retention

**Status:** RESOLVED — New function `anonymize_old_audit_logs()` in
`retention_cleanup.py` anonymizes `request_context` after `AUDIT_RETENTION_DAYS`
(default: 365 days, configurable via env var). Integrated as phase 4 in
the retention cleanup flow.

### RESOLVED: Hardcoded ingestion URL in MCP server

**Status:** RESOLVED — `INGESTION_URL` is now read from environment variable
(default: `http://ingestion:8081`). All ingestion calls in `server.py`
use the configurable URL.
