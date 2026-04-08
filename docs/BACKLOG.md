# Powerbrain Backlog

Open tasks, prioritized. Last updated: 2026-04-08.

---

## Backlog — Policy Management Roadmap

### B-10: OPAL Integration (Option B)
**Priority:** Low (after B-01)
**Effort:** ~2–3 days

OPAL for realtime policy + data sync instead of OPA bundle polling.

- [ ] OPAL server + client as Docker services
- [ ] Git watcher on Forgejo `pb-policies` repo
- [ ] WebSocket-based push on policy changes

### B-11: Policy Management Web UI (Option C)
**Priority:** Low (after B-01, optional after B-10)
**Effort:** ~4–5 days

Lightweight web frontend for policy-data management.

- [ ] Policy-data editor (JSON forms)
- [ ] JSON Schema validation in the frontend
- [ ] OPA dry-run / policy preview
- [ ] Commit to Forgejo (versioning + audit trail)
- [ ] Role-based access (admin only)

### B-12: MCP tool `manage_policies` (CRUD)
**Priority:** Medium
**Effort:** ~1 day

MCP tool for reading/writing policy data via the OPA Data API.

- [ ] `manage_policies` tool: read/update policy data sections
- [ ] JSON Schema validation before write access
- [ ] OPA admin-only access control

---

## Backlog — Reranking

### B-13: boost_corrections — prefer correction documents in reranking
**Priority:** Low
**Effort:** ~0.5 day

timecockpit-mcp stores user-corrected timesheet descriptions as documents with
`metadata.isCorrection: true` in the KB (source_type `timesheet`). These represent
validated, high-quality texts and should be preferred in similarity searches.

New reranking parameter `boost_corrections` (analogous to `boost_same_author`):
- Heuristic boost on documents with `metadata.isCorrection == true`
- Recommended default boost: 0.1–0.2
- Configurable via `rerank_options` in the `search_knowledge` call

- [ ] Implement `boost_corrections` parameter in the reranking pipeline
- [ ] Consider `isCorrection` metadata field in scoring
- [ ] Tests: correction document is ranked higher than identical text without the flag

---

## Backlog — PII & Data Protection

### B-30: graph_query returns unscanned cleartext names
**Priority:** Medium
**Effort:** ~0.5–1 day

`graph_query` returns knowledge graph nodes from Apache AGE that were created with cleartext names at import time (user nodes with `firstname`, `lastname`, `email`). This data was not pseudonymized by the PII ingestion pipeline. Powerbrain is declared as `pii_status: scanned`, which strictly speaking does not apply to `graph_query`.

**Options:**
- [ ] A: Change Powerbrain to `pii_status: mixed` and declare `graph_query` as unscanned
- [ ] B: Pseudonymize graph node properties at import time (analogous to Qdrant dual storage)
- [ ] C: Pseudonymize `graph_query` results in the MCP server before returning them

### B-31: Ingestion does not pseudonymize metadata
**Priority:** Medium
**Effort:** ~1 day

The ingestion pipeline scans and pseudonymizes the `source` text (which is embedded), but the `metadata` object remains unchanged. Import scripts write cleartext names there (`userName`, `customerName`, `authorEmail`). This metadata ends up unscanned in the Qdrant payload and PostgreSQL and is returned with `search_knowledge` results.

- [ ] Extend PII scan to configurable metadata fields
- [ ] Or: filter metadata fields containing PII on output (OPA `fields_to_redact`)

---

## Backlog — EU AI Act Compliance (August 2026)

Requirements from Art. 9–15 EU AI Act for high-risk AI systems.
Powerbrain itself is not a high-risk system, but deployers in regulated industries
(finance, healthcare, HR) need these capabilities from their context infrastructure.

### B-40: Tamper-Resistant Audit Logs (Art. 12 Record-Keeping)
**Priority:** High
**Effort:** ~2 days
**EU AI Act:** Art. 12 — automatic, tamper-resistant logging

Current state: audit log in PostgreSQL exists (`init-db/001_schema.sql`),
but no cryptographic integrity protection.

- [ ] Hash chain for audit log entries (SHA-256, each entry references the previous entry's hash)
- [ ] Append-only constraint on the audit table (no UPDATE/DELETE via RLS)
- [ ] Integrity verification: MCP tool or CLI command to verify the hash chain
- [ ] Configurable log retention with policy (`data.json`: `audit_retention_days`)
- [ ] Export function for audit logs (JSON/CSV) for external archival

### B-41: Transparency Report / Model Card Endpoint (Art. 13 Transparency)
**Priority:** High
**Effort:** ~1.5 days
**EU AI Act:** Art. 13 — understandable information about system behavior for deployers

- [ ] `GET /transparency` endpoint on the MCP server: machine-readable report (JSON)
  - System purpose and operational limits
  - Models in use (embedding, reranker, summarization) with versions
  - Active OPA policies and classification levels
  - PII processing status and pseudonymization method
  - Data sources and last update
- [ ] MCP tool `get_system_info` for agents
- [ ] Versioning of the report (new snapshot on config changes)

### B-42: Human Oversight Controls (Art. 14 Human Oversight)
**Priority:** High
**Effort:** ~2–3 days
**EU AI Act:** Art. 14 — human oversight for risk minimization

Powerbrain currently has no mechanism for human intervention.

- [ ] Approval queue: OPA policy can set results to `pending_review` instead of delivering directly
  - New classification `requires_approval` in `data.json`
  - Deployer decides via policy which data/actions need review
- [ ] MCP tool `review_pending`: display + approve/reject of pending results
- [ ] Kill switch: `POST /circuit-breaker` — deactivates all data delivery immediately
  - Persistent state (survives restart)
  - Admin role only
  - Audit log entry on activation/deactivation
- [ ] Rate-based auto alert: on unusually high access to `confidential`/`restricted` data

### B-43: Data Quality Validation at Ingestion (Art. 10 Data Governance)
**Priority:** Medium
**Effort:** ~1.5 days
**EU AI Act:** Art. 10 — data must be relevant, representative, error-free, and complete

- [ ] Schema validation: check required fields per `source_type` (JSON Schema)
- [ ] Duplicate detection: embedding similarity check against existing documents (configurable threshold)
- [ ] Quality score per document (length, language detected, PII ratio, encoding errors)
- [ ] Ingestion report: summary per batch (accepted/rejected/warnings)
- [ ] OPA policy `pb.ingestion.quality_gate`: configurable minimum score

### B-44: Risk Management Documentation (Art. 9 Risk Management)
**Priority:** Medium
**Effort:** ~1 day
**EU AI Act:** Art. 9 — documented, ongoing risk management across the entire lifecycle

- [ ] `docs/risk-management.md` — template for deployers:
  - Identified risks of the context pipeline (hallucination from wrong context, PII leaks, policy bypass)
  - Mitigation measures (OPA policies, PII vault, reranking quality)
  - Residual risks and recommended deployer measures
- [ ] Automated risk indicator on `/health` endpoint:
  - OPA policy age (stale policies = risk)
  - PII scanner status (disabled = risk)
  - Reranker availability (down = quality risk)
  - Audit log integrity (hash chain status)

### B-45: Accuracy Monitoring and Drift Detection (Art. 15 Accuracy/Robustness)
**Priority:** Medium
**Effort:** ~2 days
**EU AI Act:** Art. 15 — accuracy, robustness, and cybersecurity across the entire lifecycle

Current state: `submit_feedback` + `get_eval_stats` exist, but no systematic monitoring.

- [ ] Automated quality metrics per time window (sliding):
  - Average feedback score
  - Share of searches without relevant results (empty results / low reranker scores)
  - Reranker score distribution (drift indicator)
- [ ] Alerting on quality drift: Prometheus alert when avg_score drops below threshold
- [ ] Embedding drift check: periodic comparison of new embeddings against a reference set
- [ ] Dashboard panel in Grafana: retrieval quality over time

### B-46: Technical Documentation Generator (Art. 11 Annex IV)
**Priority:** Low
**Effort:** ~1.5 days
**EU AI Act:** Art. 11 + Annex IV — detailed technical documentation

- [ ] CLI command / MCP tool `generate_compliance_doc`:
  - Automatically collects: active OPA policies, model versions, collection stats, PII config
  - Generates Annex-IV-compliant template (Markdown/PDF)
  - Sections: system purpose, data sources, training/embedding models, risk assessment, monitoring metrics
- [ ] Versioned output in `docs/compliance/` with date
- [ ] Diff view on changes (what has changed since the last version)

---

## Backlog — Technical Debt

### B-20: Clean up PipelineStep mock in proxy.py
**Priority:** Low
**Effort:** ~0.5 day

The `except ImportError` fallback defines its own `PipelineStep`, which can diverge from the original in `shared/telemetry.py`.

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

See also `docs/KNOWN_ISSUES.md` for all resolved issues (sprints 1–5).
See `docs/plans/` for completed feature implementations.
