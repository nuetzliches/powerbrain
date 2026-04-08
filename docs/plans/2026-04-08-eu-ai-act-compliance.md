# EU AI Act Compliance — Implementation Plan

**Date:** 2026-04-08 (review-updated)
**Backlog:** B-40 through B-46 + pb-worker
**EU AI Act Deadline:** August 2, 2026
**Estimated Effort:** ~13 days (parallelizable to ~9 days)

> **Review update (2026-04-08):** Plan verified against the current code. Migration numbers corrected (014–017 instead of 013–016), `pgcrypto` activation added, new scope addition `pb-worker` maintenance container, and finalized design decisions for all items. Details in `docs/plans/2026-04-08-eu-ai-act-compliance-review.md` (alignment document).

## Context

Powerbrain as a context engine is not a high-risk AI system per se, but deployers in regulated industries (finance, healthcare, HR) need Art. 9–15 capabilities from their infrastructure. This feature set turns Powerbrain into a "compliance-ready building block" for high-risk systems.

## Phases (dependency-based)

```
Phase 1 (parallel):  B-40 Audit Hash Chain    |  B-44 Risk Management Docs
Phase 2 (parallel):  B-41 Transparency        |  B-43 Data Quality
Phase 3:             B-42 Human Oversight     |  pb-worker skeleton
Phase 4:             B-45 Accuracy Monitoring (uses pb-worker)
Phase 5:             B-46 Compliance Doc Generator
```

B-41 reads audit integrity status from B-40. B-42 needs the transparency endpoint for kill-switch reporting. B-45 extends the ingestion quality metrics from B-43. B-46 collects outputs from all previous items. `pb-worker` is set up as an empty skeleton before B-45 so that the B-40 cleanup, B-42 timeout, and B-45 metrics refresh jobs can plug in.

---

## B-40: Tamper-Resistant Audit Logs (Art. 12) — HIGH, ~2 days

**EU AI Act Art. 12:** Automatic, tamper-resistant logging.

**Design:** Hash chain via PostgreSQL BEFORE INSERT trigger. Each audit entry stores `SHA-256(prev_hash || id || agent_id || action || resource_id || created_at)`. Application code (`log_access()`) is unchanged — the trigger is transparent.

**Concurrency:** Parallel INSERTs are serialized via `pg_advisory_xact_lock(<audit_lock_id>)` in the trigger (only audit writes, other transactions remain parallel).

**Retention / GDPR:** "Checkpoint + prune" instead of direct hard delete.
1. `pb_verify_audit_chain(start_id, end_id)` verifies the range to be deleted
2. On success: entry in new table `audit_archive` (archived_at, last_entry_id, last_verified_hash, row_count, chain_valid)
3. Hard delete of rows up to `last_entry_id`
4. New chain continues with `prev_hash = audit_archive.last_verified_hash` → mathematically continuous despite deleted intermediate links

**Existing architecture (preserved):**
- `agent_access_log` table (`init-db/001_schema.sql`)
- RLS: `mcp_app` = INSERT-only, `mcp_auditor` = SELECT-only (`init-db/008_audit_rls.sql`)
- `log_access()` in `mcp-server/server.py:609` with PII scanning

**Files:**

| File | Action | Description |
|-------|--------|-------------|
| `init-db/014_audit_hashchain.sql` | NEW | `CREATE EXTENSION IF NOT EXISTS pgcrypto`, `prev_hash`/`entry_hash` columns on `agent_access_log`, advisory-lock BEFORE INSERT trigger, `audit_archive` table (RLS like 008), `pb_verify_audit_chain()` and `pb_audit_checkpoint_and_prune()` functions |
| `opa-policies/pb/data.json` | MODIFY | New section `"audit": {"retention_days": 365, "advisory_lock_id": 847291}` |
| `opa-policies/pb/policy_data_schema.json` | MODIFY | Schema for the `audit` section |
| `mcp-server/server.py` | MODIFY | 2 MCP tools: `verify_audit_integrity` (admin, optional range), `export_audit_log` (admin, JSON/CSV, filters: range/agent_id/action, max rows) |
| `mcp-server/tests/test_audit_integrity.py` | NEW | Unit tests (chain verify, tamper detection, checkpoint continuation) |

**Reuse:** `log_access()` stays unchanged — the trigger acts transparently beneath it. `008_audit_rls.sql` as a template for `audit_archive` RLS. The actual `audit_retention_cleanup` job runs in the new `pb-worker` (see below), not as a DB cron.

---

## B-41: Transparency Report Endpoint (Art. 13) — HIGH, ~1.5 days

**EU AI Act Art. 13:** Understandable information about system behavior for deployers.

**Design:** `GET /transparency` as an auth-required Starlette route. Any valid `pb_` API key may query it (Art. 13 targets deployers, not the general public — prevents info leaks). The route is **not** added to `AUTH_BYPASS_PATHS`. Report is cached (60s TTL). `report_version` = SHA-256 over model env vars + OPA config hash + collection list; cache refresh on the next hit after TTL.

**Report contents:**
- System purpose and operational limits
- Model versions (embedding, reranker, summarization) from env vars
- Active OPA policies from `GET {OPA_URL}/v1/data/pb/config`
- Qdrant collection stats
- PII scanner config from ingestion `/health`
- Audit chain integrity from B-40

**Files:**

| File | Action | Description |
|-------|--------|-------------|
| `mcp-server/server.py` | MODIFY | `transparency_report()` handler, route registration (~line 2147). Auth stays active, i.e. **no** entry in `AUTH_BYPASS_PATHS`. Additional MCP tool `get_system_info` |
| `mcp-server/tests/test_transparency.py` | NEW | Unit tests (auth required, cache invalidation on config change) |

**Reuse:** OPA query via the existing `check_opa_policy()`. Qdrant client already initialized. `/health` pattern as a template — but without auth bypass.

---

## B-42: Human Oversight Controls (Art. 14) — HIGH, ~2.5 days

**EU AI Act Art. 14:** Human oversight for risk minimization.

**Design:**
1. **Circuit breaker (global kill switch):** Single-row table `pb_circuit_breaker_state` (survives restart), in-memory cache (5s TTL). `POST /circuit-breaker` (admin auth) toggles the switch. When active: all data tools (`search_knowledge`, `query_data`, `get_code_context`, `get_document`) return an error with `reason` at the very beginning of `_dispatch()` (server.py:1206). No per-role/classification granularity — deliberately simple Art. 14 semantics.
2. **Approval queue (async flow):** `pending_reviews` table. OPA policy `pb.oversight.requires_approval` decides per request (role × classification × action) whether review is required. If so, the search path creates a review row and immediately returns `{status: "pending", review_id: <uuid>}`. The agent polls using a new tool `get_review_status`. An admin decides via the tool `review_pending` (approve/deny). On approval, the original query is stored in the review record and answered with results on the next `get_review_status` poll.
3. **Timeout / escalation:** `pending_review_timeout` config. Job in `pb-worker` sets expired reviews to `expired` and fires a Prometheus alert.
4. **Anomaly alert:** Prometheus counter `pb_confidential_access_total`, alert on unusually high access (TokenBucket pattern as template).

**Files:**

| File | Action | Description |
|-------|--------|-------------|
| `init-db/015_human_oversight.sql` | NEW | `pending_reviews` (uuid, agent_id, agent_role, tool, arguments JSONB, classification, status, decision_by, decision_at, expires_at), `pb_circuit_breaker_state` (single row, active BOOL, reason TEXT, set_by, set_at), RLS like 008 |
| `opa-policies/pb/oversight.rego` | NEW | `requires_approval` rule, data from `data.pb.human_oversight` |
| `opa-policies/pb/oversight_test.rego` | NEW | OPA tests |
| `opa-policies/pb/data.json` | MODIFY | `"human_oversight": {requires_approval_matrix, pending_review_timeout_minutes, max_pending_per_agent}` section |
| `opa-policies/pb/policy_data_schema.json` | MODIFY | Schema for the new section |
| `mcp-server/server.py` | MODIFY | Circuit-breaker gate at the start of `_dispatch()` (~1206), `POST/GET /circuit-breaker` route, two MCP tools `review_pending` (admin) + `get_review_status` (all roles), approval interception in the search path |
| `monitoring/alerting_rules.yml` | MODIFY | `HighConfidentialAccessRate`, `PendingReviewExpired` alerts |
| `mcp-server/tests/test_human_oversight.py` | NEW | Unit tests (kill switch blocks dispatch, async flow end-to-end, timeout) |

**Reuse:** TokenBucket pattern (server.py:201) as a template for rate-based alerting. RLS pattern from `008_audit_rls.sql`. `pb-worker` for the timeout job.

---

## B-43: Data Quality Validation at Ingestion (Art. 10) — MEDIUM, ~1.5 days

**EU AI Act Art. 10:** Data must be relevant, representative, error-free, and complete.

**Design:** Quality score (0.0–1.0) from 5 weighted factors:
- Text length (0.25) — too short/long is penalized
- Language detection confidence (0.20)
- PII ratio (0.20) — high PII ratio = lower score
- Encoding cleanliness (0.15) — mojibake, control characters
- Metadata completeness (0.20) — required fields per source_type

Duplicate detection via cosine similarity of the first-chunk embedding (configurable threshold, default 0.95).

**Gate behavior:** Blocking. Documents below `min_quality_score` are rejected (OPA policy `pb.ingestion.quality_gate`). Threshold configurable per `source_type` (e.g. `code` looser than `contracts`).

**Files:**

| File | Action | Description |
|-------|--------|-------------|
| `init-db/016_data_quality.sql` | NEW | `quality_score` (REAL) + `quality_details` (JSONB) columns on `documents_meta`, index on quality_score |
| `ingestion/quality.py` | NEW | `compute_quality_score()`, `check_duplicate()`, schema validation |
| `ingestion/quality_schemas/` | NEW | JSON schemas per source_type |
| `ingestion/ingestion_api.py` | MODIFY | Quality pipeline between PII scan and embedding in `ingest_text_chunks()` (~line 478). On gate fail: rejected log + early return with `{"status": "rejected", "reason": ..., "quality_score": ...}` |
| `opa-policies/pb/ingestion.rego` | NEW | `quality_gate` rule, reads `min_quality_score` map from `data.pb.ingestion` |
| `opa-policies/pb/ingestion_test.rego` | NEW | OPA tests |
| `opa-policies/pb/data.json` | MODIFY | `"ingestion": {"min_quality_score": {"default": 0.6, "code": 0.4, "contracts": 0.8}, "duplicate_threshold": 0.95}` section |
| `opa-policies/pb/policy_data_schema.json` | MODIFY | Schema for the `ingestion` section |
| `ingestion/tests/test_quality.py` | NEW | Unit tests |

**Reuse:** `EmbeddingProvider.embed_batch()` from `shared/llm_provider.py` for the duplicate check. PII scanner results already available in the pipeline. `check_opa_privacy()` pattern as a template for `check_opa_ingestion_quality()`.

---

## B-44: Risk Management Documentation (Art. 9) — MEDIUM, ~1 day

**EU AI Act Art. 9:** Documented, ongoing risk management.

**Design:** Enhanced `/health` returns structured JSON with risk indicators **only when** the `Accept: application/json` header is set. Plain-text `"ok"` remains the default for Docker/LB health checks (backwards compatible).

**Risk indicators:**
- OPA reachable (critical if down)
- PII scanner status (high if disabled)
- Reranker available (medium if down)
- Audit chain integrity (critical if broken)
- Circuit breaker state (info)
- Feedback score (warning if <2.5)

**Risk register (`docs/risk-management.md`):** Concrete Powerbrain risk register (not a generic template). Covers at least: LLM hallucination, PII leak in the pseudo path, embedding drift, audit chain break, OPA outage, vault compromise, input injection via search texts. Per risk: description, likelihood, impact, mitigation (implemented), residual risk, deployer responsibility.

**Files:**

| File | Action | Description |
|-------|--------|-------------|
| `docs/risk-management.md` | NEW | Concrete Powerbrain risk register (Art. 9) with ≥7 risks and mitigations |
| `mcp-server/server.py` | MODIFY | Extend `health_check()` (~line 2012), content negotiation on `Accept` header |
| `mcp-server/tests/test_health_risk.py` | NEW | Unit tests (plain-text default, JSON via Accept, indicator values) |

**Reuse:** Health check pattern already exists. Qdrant/OPA/reranker connectivity checks exist in various functions. Audit chain check via `pb_verify_audit_chain()` from B-40.

---

## B-45: Accuracy Monitoring and Drift Detection (Art. 15) — MEDIUM, ~2 days

**EU AI Act Art. 15:** Accuracy, robustness, and cybersecurity across the entire lifecycle.

**Design:** Windowed metrics (1h, 24h, 7d) via SQL view `v_feedback_windowed` on `search_feedback`. Metrics refresh runs **in the new `pb-worker` container** (not in the mcp-server process) every 5 minutes. Embedding drift check compares new vectors against a reference set.

**Reference set:** Deployment snapshot. On the first `pb-worker` start, the worker samples N documents per collection from Qdrant and stores their embeddings in `embedding_reference_set` as the baseline. Reproducible via `ingestion/snapshot_service.py`. Re-sampling only happens manually (admin tool or explicit re-seed).

**Drift threshold per collection** in `data.json` (`drift.thresholds: {pb_general: 0.08, pb_code: 0.12, pb_rules: 0.05}`).

**Files:**

| File | Action | Description |
|-------|--------|-------------|
| `init-db/017_accuracy_monitoring.sql` | NEW | Windowed metrics view `v_feedback_windowed`, `embedding_reference_set` table (collection, doc_id, embedding VECTOR, created_at) |
| `worker/jobs/accuracy_metrics.py` | NEW | Job: query metrics view, push Prometheus gauges, call `drift_check.compute_drift()` per collection |
| `mcp-server/server.py` | MODIFY | New Prometheus gauges (registered for inspection via `/metrics/json`), extend `get_eval_stats` with windowed values |
| `monitoring/alerting_rules.yml` | MODIFY | `QualityDrift`, `HighEmptyResultRate`, `RerankerScoreDrift` alerts |
| `monitoring/grafana-dashboards/pb-accuracy.json` | NEW | Accuracy dashboard |
| `shared/drift_check.py` | NEW | Embedding drift comparison function (cosine centroid distance, per-collection threshold) |
| `worker/tests/test_accuracy_job.py` | NEW | Unit tests |

**Reuse:** `get_eval_stats()` already exists (server.py:1714). `pb_feedback_avg_rating` gauge as a pattern. `MetricsAggregator` from `shared/telemetry.py`. `ingestion/snapshot_service.py` for the baseline seed.

---

## B-46: Technical Documentation Generator (Art. 11 / Annex IV) — LOW, ~1.5 days

**EU AI Act Art. 11 + Annex IV:** Detailed technical documentation.

**Design:** Separate module `compliance_doc.py` queries all data sources (OPA, Qdrant, PostgreSQL, `/transparency`) and renders the Annex IV template as Markdown (**EN only** — standard for EU AI Act documents). Admin-only MCP tool with parameter `output_mode: "inline" | "file"` (default `inline`). For `file`, it writes to a configurable path and returns the path.

**Files:**

| File | Action | Description |
|-------|--------|-------------|
| `mcp-server/compliance_doc.py` | NEW | `generate_annex_iv_doc(output_mode: Literal["inline","file"])` function, EN template |
| `mcp-server/server.py` | MODIFY | `generate_compliance_doc` tool (admin-only), parameter `output_mode` |
| `mcp-server/tests/test_compliance_doc.py` | NEW | Unit tests (inline default, file mode, all Annex IV sections populated) |

**Reuse:** `/transparency` endpoint from B-41 as the primary data source.

---

## pb-worker: Maintenance Container (scope addition) — ~1 day

**Motivation:** There is currently no home for periodic maintenance jobs. `retention_cleanup.py` is only a CLI script with no scheduler. Several new items (B-40 audit cleanup, B-45 metrics refresh, B-42 review timeout) need a scheduler. A dedicated container consolidates maintenance.

**Service:** `pb-worker` — new Docker service, shares the image base with `ingestion` (same DB / Qdrant clients + APScheduler). No open port (internal metrics endpoint optional).

**Scheduler:** `APScheduler` (AsyncIOScheduler) inside the Python process.

**Jobs:**

| Job | Interval | Source | Description |
|---|---|---|---|
| `accuracy_metrics_refresh` | every 5 min | B-45 | Read view `v_feedback_windowed`, push gauges, run `drift_check` per collection |
| `audit_retention_cleanup` | daily 03:00 | B-40 | `pb_verify_audit_chain` → `pb_audit_checkpoint_and_prune` |
| `gdpr_retention_cleanup` | daily 02:00 | migrated existing `retention_cleanup.py` | Move logic from the CLI into a worker job; `--execute` as default in the container, CLI stays for manual dry runs |
| `pending_review_timeout` | hourly | B-42 | Set expired reviews to `expired`, fire alert |

**Files:**

| File | Action | Description |
|---|---|---|
| `worker/scheduler.py` | NEW | APScheduler setup + job registration + lifespan |
| `worker/jobs/accuracy_metrics.py` | NEW | B-45 job |
| `worker/jobs/audit_retention.py` | NEW | B-40 job |
| `worker/jobs/gdpr_retention.py` | NEW | Move logic from `ingestion/retention_cleanup.py` here |
| `worker/jobs/pending_review_timeout.py` | NEW | B-42 job |
| `worker/Dockerfile` | NEW | Base `python:3.12-slim`, installs `worker/requirements.txt` + `shared/` |
| `worker/requirements.txt` | NEW | `apscheduler`, `asyncpg`, `qdrant-client`, `prometheus-client`, `httpx` |
| `worker/tests/test_jobs.py` | NEW | Unit tests per job |
| `ingestion/retention_cleanup.py` | MODIFY | Remains as a thin wrapper / CLI around the worker job (import from `worker/jobs/gdpr_retention.py`) |
| `docker-compose.yml` | MODIFY | New service `pb-worker` with `depends_on: postgres, qdrant, opa`, no ports, `pb-net` network |

**Reuse:** `build_postgres_url()` from `shared/config.py`, `EmbeddingProvider` from `shared/llm_provider.py`, `init_telemetry()` from `shared/telemetry.py`.

---

## Summary

| Item | Effort | Phase | Migration | New OPA policy | New MCP tools | New endpoints |
|------|--------|-------|-----------|----------------|----------------|----------------|
| B-40 | 2d | 1 | 014 | — | 2 | — |
| B-41 | 1.5d | 2 | — | — | 1 | `GET /transparency` (auth) |
| B-42 | 2.5d | 3 | 015 | `pb.oversight` | 2 | `POST/GET /circuit-breaker` |
| B-43 | 1.5d | 2 | 016 | `pb.ingestion` | — | — |
| B-44 | 1d | 1 | — | — | — | Enhanced `/health` |
| B-45 | 2d | 4 | 017 | — | — | — |
| B-46 | 1.5d | 5 | — | — | 1 | — |
| pb-worker | 1d | 3 | — | — | — | — |

**New migrations:** 014–017 (4 SQL files)
**New OPA policies:** 2 (`pb.oversight`, `pb.ingestion`)
**New MCP tools:** 6 (`verify_audit_integrity`, `export_audit_log`, `get_system_info`, `review_pending`, `get_review_status`, `generate_compliance_doc`)
**New endpoints:** 3 (`/transparency` auth-required, `/circuit-breaker`, enhanced `/health`)
**New services:** 1 (`pb-worker` maintenance container)

## Verification

```bash
# Unit tests (all new tests)
PYTHONPATH=.:mcp-server:ingestion:reranker:pb-proxy \
python -m pytest mcp-server/tests/test_audit_integrity.py \
                 mcp-server/tests/test_transparency.py \
                 mcp-server/tests/test_human_oversight.py \
                 mcp-server/tests/test_health_risk.py \
                 mcp-server/tests/test_accuracy_monitoring.py \
                 mcp-server/tests/test_compliance_doc.py \
                 ingestion/tests/test_quality.py \
                 -v

# OPA tests (including new policies)
docker exec pb-opa /opa test /policies/pb/ -v

# Integration: audit hash chain (auth required)
curl -s -H "Authorization: Bearer pb_admin_key" \
  localhost:8080/transparency | jq '.audit_integrity'

# Integration: circuit breaker
curl -X POST localhost:8080/circuit-breaker \
  -H "Authorization: Bearer pb_admin_key" \
  -d '{"active": true, "reason": "test"}'

# Integration: health with risk indicators
curl -H "Accept: application/json" localhost:8080/health | jq '.risk_level'
```
