# Powerbrain Backlog

Last updated: 2026-04-09. All backlog items completed or closed.

---

## Backlog — Technical Debt

### B-21: ~~Forgejo Workflows → internal infra repo~~
**Done** — `.forgejo/` stays in the repo (coexistence model). GitHub ignores the directory.

### B-22: ~~GitHub Actions CI (pre-public)~~
**Done** — `.github/workflows/pr-validate.yml` with 3 jobs (unit-tests, opa-tests, docker-build).

---

## Done

### ✅ B-01: OPA Policy-Data Extraction (2026-03-27)
All business data extracted from 5 Rego files into `opa-policies/pb/data.json`.
Rego now contains logic only. JSON Schema validation. 85 OPA tests (previously: 33).

### ✅ B-02: E2E Smoke Tests for pb-proxy (2026-03-27)
`tests/integration/e2e/test_proxy_smoke.py` — auth, OPA policy, tool injection, health/models/metrics.

### ✅ B-23: Secrets/URLs Audit (2026-03-27)
`build-images.sh` parameterized (REGISTRY as required var), doc paths cleaned up, `.env.example` verified.

### ✅ B-24: LICENSE file (2026-03-27)
Apache License 2.0 added.

### ✅ B-03: Reranker Provider Integration Test (2026-03-27)
`mcp-server/tests/test_reranker_integration.py` — 7 tests for provider switching (powerbrain/tei/cohere), graceful fallback on timeout/connection error/500.

### ✅ B-40: Tamper-Resistant Audit Logs — Art. 12 (2026-04-08)
SHA-256 hash chain on `agent_access_log` via BEFORE INSERT trigger (`014_audit_hashchain.sql`). Advisory-lock serialization, `pb_verify_audit_chain()`, checkpoint-and-prune for GDPR retention, `pb_verify_audit_chain_tail()` for lightweight health checks. MCP tools `verify_audit_integrity` + `export_audit_log` (admin-only). Backfill for existing rows.

### ✅ B-41: Transparency Report Endpoint — Art. 13 (2026-04-08)
`GET /transparency` (auth-required, cached). Reports system purpose, model versions, OPA policies, Qdrant stats, PII config, audit chain integrity. MCP tool `get_system_info`.

### ✅ B-42: Human Oversight Controls — Art. 14 (2026-04-08)
Circuit breaker (`POST/GET /circuit-breaker`, persistent single-row table, 5s cache). Approval queue (`pending_reviews` table, OPA `pb.oversight.requires_approval`). MCP tools `review_pending` + `get_review_status`. Timeout job in pb-worker. Prometheus alerts `HighConfidentialAccessRate` + `PendingReviewExpired`. Migration `015_human_oversight.sql`.

### ✅ B-43: Data Quality Validation — Art. 10 (2026-04-08)
Quality score (0.0–1.0) from 5 weighted factors in `ingestion/quality.py`. Duplicate detection via cosine similarity. OPA policy `pb.ingestion.quality_gate` with per-source-type thresholds. Rejection log table. Migration `016_data_quality.sql`.

### ✅ B-44: Risk Management Documentation — Art. 9 (2026-04-08)
`docs/risk-management.md` with concrete risk register (7+ risks). Content-negotiated `/health`: plain "ok" default, structured risk-indicator JSON via `Accept: application/json`.

### ✅ B-45: Accuracy Monitoring + Drift Detection — Art. 15 (2026-04-08)
Windowed feedback metrics view `v_feedback_windowed` (1h/24h/7d). Embedding drift via `shared/drift_check.py` (cosine centroid distance, per-collection thresholds). pb-worker job refreshes metrics every 5 min. Reference set in `embedding_reference_set` table. Prometheus alerts `QualityDrift`, `HighEmptyResultRate`, `RerankerScoreDrift`. Migration `017_accuracy_monitoring.sql`.

### ✅ B-46: Technical Documentation Generator — Art. 11 / Annex IV (2026-04-08)
`mcp-server/compliance_doc.py` generates Annex-IV-compliant Markdown. MCP tool `generate_compliance_doc` (admin-only, `output_mode: inline | file`).

### ✅ pb-worker: Maintenance Container (2026-04-08)
`worker/scheduler.py` with APScheduler. 4 jobs: `accuracy_metrics_refresh` (5 min), `audit_retention_cleanup` (daily 03:00), `gdpr_retention_cleanup` (daily 02:00), `pending_review_timeout` (hourly). Docker service `pb-worker` (internal port 8083, no external).

### ✅ B-30: graph_query PII masking (2026-04-09)
PII-scan graph_query/graph_mutate results via `/scan` endpoint. Recursive walker masks firstname, lastname, email, phone, name. Graceful degradation on scanner failure.

### ✅ B-31: Metadata PII redaction (2026-04-09)
Redact PII-sensitive metadata keys in search_knowledge/get_code_context based on configurable mapping (`pii_metadata_fields` in pii_config.yaml) + OPA `fields_to_redact` policy. Fail-closed on OPA failure.

### ✅ B-12: manage_policies MCP tool (2026-04-09)
Admin-only tool with list/read/update actions for OPA policy data sections. JSON Schema validation before writes, cache invalidation, audit logging. `jsonschema` dependency added.

### ✅ B-13: boost_corrections in reranking (2026-04-09)
New `boost_corrections` parameter in `rerank_options`. Boosts documents with `metadata.isCorrection: true` by a configurable score. Analogous to existing `boost_same_author`.

### ✅ B-20: PipelineStep mock cleanup (2026-04-09)
Cleaned up `except ImportError` fallback in `pb-proxy/proxy.py`. Stub now matches `shared/telemetry.PipelineStep` signature including `to_dict()` method.

### ✅ B-10: OPAL Integration (2026-04-09)
opal-server + opal-client as Docker Compose profile (`--profile opal`). Watches a git repo for policy changes and pushes to OPA in real-time via WebSocket. Configurable via `OPAL_POLICY_REPO_URL`. Replaces the commented-out OPA bundle polling.

### ⏭️ B-11: Policy Management Web UI — Won't Do (2026-04-09)
MCP is the single access channel by design. The `manage_policies` MCP tool (B-12) covers the use case. A dedicated web UI would duplicate functionality and expand the attack surface without sufficient benefit.

See also `docs/KNOWN_ISSUES.md` for all resolved issues (sprints 1–5).
See `docs/plans/` for completed feature implementations.
