# Risk Management — Powerbrain Context Engine

**Scope:** EU AI Act Art. 9 risk register for the Powerbrain open-source context engine.
**Status:** living document — updated whenever a risk, mitigation, or residual assessment changes.
**Target audience:** Deployers who integrate Powerbrain into a high-risk AI system and need to inherit documented, technically-enforced risk controls.

## Positioning

Powerbrain is **not itself** a high-risk AI system. It is a compliance-ready building block used by Deployers to feed policy-filtered enterprise knowledge into their own AI systems (agents, copilots, decision-support tools). Deployers remain responsible for the overall AI Act obligations; Powerbrain supplies the infrastructure-level controls called out in Art. 9–15 and documents the residual risks they must manage.

This register identifies the concrete, code-level risks of running Powerbrain and the mitigations implemented in this repository. Each entry states:

- **Risk** — what can go wrong
- **Likelihood / Impact** — qualitative (low/medium/high)
- **Mitigation (implemented)** — what the code does about it today
- **Residual risk** — what the Deployer must still handle

## Risk Register

### R-01 Incorrect or hallucinated agent output
- **Description:** Downstream LLMs summarize or cite Powerbrain results; hallucinations or outdated data can cause wrong business decisions. Powerbrain does not generate content itself but feeds the generators.
- **Likelihood:** High
- **Impact:** Medium — depends on the Deployer's use case
- **Mitigation (implemented):**
  - OPA-controlled summarization policy (`pb.summarization`): viewer role denied, confidential requires summary-only, restricted limited to `brief` detail
  - Retrieval pipeline (Qdrant → OPA filter → cross-encoder rerank) returns ranked, classification-filtered evidence — summaries are grounded in the filtered set
  - Feedback loop (`search_feedback` table, `get_eval_stats` tool) surfaces low-quality queries; B-45 adds accuracy drift alerts
  - Graceful degradation keeps raw chunks available when summarization fails so Deployers can always inspect the source evidence
- **Residual risk (Deployer):** Instruct users that summaries are generated; require human review for consequential decisions; track the Deployer's own LLM quality metrics alongside Powerbrain's.

### R-02 PII leak through the pseudonymization path
- **Description:** PII bypassing the Presidio scanner is embedded into Qdrant vectors and can be retrieved by roles that should not see it, breaching Art. 5 / 9 / 10 GDPR.
- **Likelihood:** Medium — depends on data source and Presidio entity coverage
- **Impact:** High
- **Mitigation (implemented):**
  - Sealed Vault dual storage (`init-db/007_pii_vault.sql`): Qdrant stores only pseudonyms, originals live in a separate `pii_vault` schema with RLS
  - `PII_SCAN_FORCED=true` default (fail-closed); OPA policy `pb.proxy.pii_scan_forced` enforces it for the chat path
  - Presidio entity list configurable via `ingestion/pii_config.yaml` and `pb.config.pii_entity_types`
  - HMAC-signed short-lived tokens gate access to vault originals (`validate_pii_access_token`)
  - Audit log records every PII-relevant access including query text scanning before persistence
  - Quality gate (B-43) rejects documents with excessive PII density at ingestion time
- **Residual risk (Deployer):** Custom PII patterns (internal customer IDs, proprietary identifiers) must be added to `pii_config.yaml`; Deployer is responsible for purpose binding and retention periods specific to their data classes.

### R-03 Audit-log tampering or chain break
- **Description:** Attacker with DB access (malicious admin, compromised service account, SQL injection) mutates or deletes `agent_access_log` rows to hide unauthorized access, violating Art. 12.
- **Likelihood:** Low — requires privileged access
- **Impact:** High — undermines all downstream compliance evidence
- **Mitigation (implemented):**
  - SHA-256 hash chain over every audit entry (migration 014), advisory-lock-serialized
  - `BEFORE UPDATE` trigger blocks in-place mutation (append-only enforcement)
  - `pb_verify_audit_chain()` function detects any row-level tampering and returns the first invalid id
  - `audit_archive` checkpoint registry preserves chain continuity across retention cleanups (`pb_audit_checkpoint_and_prune`)
  - MCP tools `verify_audit_integrity` and `export_audit_log` (admin only) surface the chain to compliance workflows
  - RLS on `agent_access_log` and `audit_archive`: `mcp_app` INSERT-only, `mcp_auditor` SELECT-only
- **Residual risk (Deployer):** Superuser access to the database is unchecked by design — Deployer must protect superuser credentials (e.g., via Docker Secrets, vault) and monitor OS-level access; off-box WORM storage of exported audit logs is recommended for regulated contexts.

### R-04 Embedding model drift / silent retrieval degradation
- **Description:** The embedding or reranker model changes (version bump, fine-tune) and retrieval quality silently degrades. Users keep seeing results, but relevance drops — no visible failure.
- **Likelihood:** Medium
- **Impact:** Medium
- **Mitigation (implemented):**
  - LLM provider abstraction (`shared/llm_provider.py`) pins model names explicitly via env vars (`EMBEDDING_MODEL`, `LLM_MODEL`)
  - Transparency endpoint (B-41) exposes active model versions as part of the system report
  - Feedback loop and accuracy metrics (B-45, planned) compare rolling windows against a deployment-snapshot reference set and fire alerts on collection-specific drift thresholds
  - Reranker provider abstraction allows Deployers to swap backends without code changes
- **Residual risk (Deployer):** Deployer owns the model upgrade schedule and must re-baseline the reference set after any intentional model change.

### R-05 OPA policy engine outage
- **Description:** OPA container crashes or `pb-policies` bundle fetch fails. Without policy decisions Powerbrain cannot enforce access control, so any default behavior is wrong — either fail-open (data leak) or fail-closed (availability outage).
- **Likelihood:** Low
- **Impact:** High (data leak) or Medium (availability)
- **Mitigation (implemented):**
  - All OPA calls in `mcp-server/server.py` default to deny on exception (fail-closed, not fail-open)
  - OPA result cache (`OPA_CACHE_TTL`) keeps recent allow decisions available through short blips but cannot outlast them
  - Docker health checks on OPA service (T1 hardening); restart policy is the Deployer's responsibility
  - Alerting (B-44 enhanced `/health`, B-45 monitoring) flags OPA reachability as a critical risk indicator
- **Residual risk (Deployer):** Deployer must run OPA under a supervisor (Docker restart, k8s liveness probe) and alert on sustained outages.

### R-06 Sealed Vault compromise
- **Description:** Attacker reads the `pii_vault` schema directly (through backup theft, DB credential leak, or misconfigured replica) and recovers PII originals that the pseudonymization system was supposed to protect.
- **Likelihood:** Low
- **Impact:** High
- **Mitigation (implemented):**
  - Vault schema is isolated with its own tables and RLS policies
  - HMAC signing secret (`VAULT_HMAC_SECRET`) loaded from Docker Secret, not env var
  - Access via short-lived token only (`validate_pii_access_token`), with OPA purpose binding
  - Art. 17 erasure is a supported first-class operation: deleting the vault mapping row makes pseudonyms in Qdrant mathematically irreversible
- **Residual risk (Deployer):** DB backups must be encrypted; replicas must use least-privilege roles; vault HMAC secret rotation is the Deployer's responsibility.

### R-07 Input injection via search queries or ingested content
- **Description:** Malicious input in user queries, ingested documents, or external MCP tool results triggers prompt injection in downstream LLMs or poisons retrieval results (e.g., injected "ignore previous instructions" phrases that get embedded and retrieved).
- **Likelihood:** Medium
- **Impact:** Medium
- **Mitigation (implemented):**
  - PII scanner runs on ingestion and on the proxy chat path
  - Structured telemetry (`_telemetry` block) surfaces suspicious-looking pipeline behavior
  - OPA classification enforcement means a poisoned `public` document cannot escalate to `confidential`
  - Audit log records every query, enabling post-hoc forensics
  - Human-oversight circuit breaker (B-42) lets admins stop retrieval system-wide when an injection campaign is detected (`POST /circuit-breaker`)
- **Residual risk (Deployer):** Deployer is responsible for content curation at ingestion time and for system prompts that defend against injection in their own LLM layer.

### R-08 Denial of service through expensive queries
- **Description:** A single agent or a misbehaving client issues retrieval storms that saturate Qdrant, the reranker, or the embedding provider, denying service to legitimate users.
- **Likelihood:** Medium
- **Impact:** Medium
- **Mitigation (implemented):**
  - Per-agent TokenBucket rate limiter (`mcp-server/server.py:201`) with per-role caps (`RATE_LIMITS_BY_ROLE`)
  - Embedding cache (`shared/embedding_cache.py`) absorbs duplicate queries
  - Graceful degradation to Qdrant ordering when reranker is overloaded
  - Metrics (`/metrics/json`, Prometheus) expose per-tool latency and error rates
- **Residual risk (Deployer):** Network-level rate limiting and quota enforcement at the API gateway remain the Deployer's responsibility.

## Risk-Indicator Surface

The enhanced `/health` endpoint (B-44, content-negotiated via `Accept: application/json`) exposes the live state of the above risks for runbook automation. See `monitoring/grafana-dashboards/` for visual surfaces.

| Indicator                   | Risks covered      | Severity mapping                                 |
|-----------------------------|--------------------|--------------------------------------------------|
| `opa_reachable`             | R-05               | critical if false                                |
| `pii_scanner_status`        | R-02, R-07         | high if disabled                                 |
| `reranker_available`        | R-04               | medium if false (graceful degradation active)    |
| `audit_chain_integrity`     | R-03               | critical if invalid                              |
| `circuit_breaker_active`    | R-07               | info (operator-initiated)                        |
| `feedback_score`            | R-01, R-04         | warning if < 2.5                                 |

## Review Cadence

- Quarterly review of this document as part of the release checklist
- Mandatory review before any change to authentication, PII handling, or audit-log code paths
- Deployers should pair this register with their own system-level risk assessment per Art. 9
