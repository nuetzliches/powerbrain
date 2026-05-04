# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.1] - 2026-05-04

A single CI hardening fix on top of v0.9.0 — no service-code changes,
no DB migrations, no breaking changes. Cuts an explicit release so
downstream infra repos can pin to a version tag.

### Fixed

- **Forgejo build pipeline now publishes version tags**
  ([build-images.sh](scripts/build-images.sh) +
  [.forgejo/workflows/build-images.yml](.forgejo/workflows/build-images.yml)).
  The Forgejo Actions build was tagging images only as `:latest` and
  `:sha-<short>`, never as `:<version>` — so consumers downstream
  (e.g. infra repos pinning the image tag) could not reference a
  release like `0.9.0`. Two changes:
  1. Workflow now triggers on `tags: ['v*']` in addition to `master`,
     and drops the `paths:` filter (release commits are typically
     CHANGELOG-only and would otherwise be skipped). The script's
     own change detection still keeps non-release runs cheap.
  2. `scripts/build-images.sh` reads the new `RELEASE_TAG` env
     (passed by the workflow as `github.ref_name` on tag pushes,
     with a `git describe --exact-match` fallback for local runs)
     and adds a `:${VERSION}` build/push tag alongside the existing
     two. Release-tagged runs also force `rebuild_all=true` so the
     full image set always carries the version tag, even when only
     CHANGELOG.md changed in the release commit.

## [0.9.0] - 2026-05-04

Closes the audit-review backlog filed against v0.8.0
(#101–#105) plus a follow-up security hardening for ingestion auth
(#126).

### ⚠ BREAKING CHANGES

- **Ingestion auth fail-closed**
  ([#126](https://github.com/nuetzliches/powerbrain/issues/126)).
  `INGESTION_AUTH_TOKEN` no longer falls back to allow-all when empty.
  With the default `AUTH_REQUIRED=true`, **mcp-server, pb-proxy,
  ingestion, and pb-worker now refuse to start** if the token is
  missing — surfacing the silent-degradation mode introduced in
  v0.8.0 (B-50, [PR #89](https://github.com/nuetzliches/powerbrain/pull/89))
  as a hard boot failure instead. Mirrors the OPA hardening from
  v0.7.1 ([PR #62](https://github.com/nuetzliches/powerbrain/pull/62))
  and the same `SKIP_*_STARTUP_CHECK` opt-out pattern.

  **Migration:** existing deployments that already provisioned
  `secrets/ingestion_auth_token.txt` (or set the env var) need no
  changes. Deployments mid-rollout with an empty token must either
  set the token, or explicitly set `AUTH_REQUIRED=false` (test/dev
  only — disables ALL auth layers, not only ingestion). Unit-test
  rigs can set `SKIP_INGESTION_AUTH_STARTUP_CHECK=true`; the standard
  `conftest.py` fixtures already do.

- **`pb_audit_force_reset()` signature gains an optional `p_purpose`
  argument and behavior changes**
  ([#101](https://github.com/nuetzliches/powerbrain/issues/101)).
  The function now writes a self-record into `agent_access_log`
  before the truncate so the reset action is captured in the
  cryptographic chain that's about to be archived. As a result,
  `archived_rows` includes the self-record (typically `+1` over the
  previous behavior), and `archived_hash` /
  `audit_archive.last_verified_hash` point to the self-record's
  `entry_hash` (the correct cryptographic snapshot of the archived
  chain) instead of the pre-call tail. Existing zero-arg and
  one-arg callers continue to work via the new default
  `p_purpose=NULL`. Test/staging tooling that asserted on
  `archived_rows` / `archived_hash` literals must update; the
  bundled live tests show the pattern.

### Added

- **`audit_archive.reset_caller` + `reset_purpose` columns**
  ([#101](https://github.com/nuetzliches/powerbrain/issues/101),
  migration `026_audit_force_reset_provenance.sql`). Captures the DB
  role that issued `pb_audit_force_reset()` and the operator-supplied
  reason. Survives in continuity mode; lost in genesis (audit_archive
  is truncated by design, but Postgres statement logs still record the
  function call).
- **`pb_ingestion_auth_enabled{service=...}` Prometheus gauge**
  ([#126](https://github.com/nuetzliches/powerbrain/issues/126)).
  Reports the boot-time decision per service (`1`=token configured,
  `0`=disabled or skipped) so dashboards can alert on degraded mode
  regardless of how the service started.
- **Live PG coverage for the audit_integrity_status worker writes**
  ([#105](https://github.com/nuetzliches/powerbrain/issues/105)).
  New `TestAuditIntegrityStatusLive` class in
  `mcp-server/tests/test_audit_integrity.py` (`PG_INTEGRATION=1`
  gated). Catches future regressions if the worker role loses the
  BYPASSRLS that today makes the UPSERT through `FORCE ROW LEVEL
  SECURITY` work.

### Fixed

- **Audit-worker logs verify result before the cache UPSERT**
  ([#102](https://github.com/nuetzliches/powerbrain/issues/102)).
  When migration 024 hadn't been applied yet (mid-rollback or partial
  setup), the verify result was silently lost: the primary
  `pb_verify_audit_chain_tail()` call succeeded, but the UPSERT into
  the missing `audit_integrity_status` table raised, and the
  exception handler tried the same UPSERT and also failed. Operators
  now see `audit chain verified: {…}` at INFO level (or
  `audit chain invalid: {…}` at ERROR) before the persistence attempt.
- **Transparency snapshot returns `total_checked: null` when the
  cache is stale** ([#104](https://github.com/nuetzliches/powerbrain/issues/104)).
  Previously returned `0`, which the Annex IV renderer dutifully
  formatted as `verified at last check: '0'` — misleading because no
  check had run at all. The snapshot's `stale` flag is the primary
  signal; `total_checked: null` is the JSON-idiomatic
  "value unknown" so consumers can't accidentally read it as zero.
  The compliance-doc renderer now displays `unknown` instead of
  `None`/`0`.
- **Test state pollution in `TestHashChainLive`**
  ([PR #131](https://github.com/nuetzliches/powerbrain/pull/131)).
  The `live_pool` fixture cleaned `agent_access_log` and
  `audit_archive` between tests but not `audit_tail`, so a later
  test's inserts chained from a stale tail and `pb_verify_audit_chain`
  walked them as broken. Fixture now resets `audit_tail` to genesis
  in both setup and teardown, mirroring `TestForceReset.live_pool`.
  Latent bug; only visible under `PG_INTEGRATION=1`.

### Documentation

- **BYPASSRLS dependency on audit-chain writes**
  ([#103](https://github.com/nuetzliches/powerbrain/issues/103)).
  Inline comments in migrations 022 (`audit_tail`) and 024
  (`audit_integrity_status`) explaining that writes succeed only via
  BYPASSRLS — `pb_admin` is `SUPERUSER` by default in the
  `pb-postgres` image. Deployments running the worker as a
  non-superuser role need an explicit INSERT/UPDATE policy or the
  `audit_integrity_status_refresh` job will silently fail and the
  transparency snapshot will degrade to stale.
- **`pb_audit_force_reset()` self-record + `p_purpose`**
  in `docs/audit-chain-migration.md`. Documents the new optional
  `p_purpose` argument and the implications of the in-chain
  self-record on the function's return values.

## [0.8.0] - 2026-04-30

Audit-chain hardening on top of the 0.7.x summarization-pool +
ingestion-auth groundwork. Five issues filed during a recovery
walkthrough after the 0.7.1 concurrency fix get resolved here, plus
a new operator helper for non-production audit resets.

### Added

- **`pb_audit_force_reset()` operator helper**
  ([#97](https://github.com/nuetzliches/powerbrain/issues/97),
  [#99](https://github.com/nuetzliches/powerbrain/pull/99)).
  Single-call replacement for the multi-statement Continuity / Genesis
  recovery procedure documented in
  `docs/audit-chain-migration.md`. Both modes acquire the
  `audit_tail` row lock (cannot race with concurrent inserts) and
  archive the current tail with `chain_valid=false` for forensic
  continuity. `continuity` preserves `audit_archive` and seeds the
  new chain from the archived hash so the verifier walks straight
  through; `genesis` additionally truncates the archive and resets
  `audit_tail.last_entry_hash` to 32 zero bytes. `SECURITY DEFINER`
  with `REVOKE EXECUTE … FROM PUBLIC` — only the DB owner / superuser
  can call it. Test/staging only — no production-environment guard
  yet (see #97 follow-up).
- **Worker-cached `audit_integrity_status`**
  ([#95](https://github.com/nuetzliches/powerbrain/issues/95),
  [#98](https://github.com/nuetzliches/powerbrain/pull/98)).
  New single-row table holding the most recent
  `pb_verify_audit_chain_tail()` result, refreshed by a new pb-worker
  job (`audit_integrity_status_refresh`, every 60 s by default,
  configurable via `AUDIT_INTEGRITY_INTERVAL_SECONDS` and
  `AUDIT_INTEGRITY_TAIL_ROWS`). The transparency report's
  `audit_integrity` field now reads from this cache so the snapshot
  reflects committed state, decoupled from the request-path INSERT
  (consumers see a `checked_at` timestamp and can judge staleness
  themselves). For a live answer, call the `verify_audit_integrity`
  MCP tool.
- **Decoupled summarization LLM pool** ([plan](docs/plans/2026-04-20-separate-summary-llm-pool.md)).
  MCP server now accepts `SUMMARIZATION_PROVIDER_URL` /
  `SUMMARIZATION_MODEL` / `SUMMARIZATION_API_KEY` so the in-pipeline
  summary call can run against its own endpoint instead of competing
  with the pb-proxy agent loop on a shared Ollama slot. Defaults to
  the existing `LLM_*` values — single-endpoint deployments need no
  change. Optional sidecar `pb-ollama-summary` ships under
  `docker compose --profile summary-llm`, exposing port 11435 on the
  host and an internal `http://ollama-summary:11434` endpoint suitable
  for a smaller distilled model (e.g. `qwen2.5:1.5b`).
  `GET /transparency` reports whether the pool is split via
  `models.llm.pool_split`. Closes the follow-up tracked in 0.7.0
  release notes.
- **Service-token authentication for the ingestion API**
  ([B-50](docs/BACKLOG.md)). The ingestion service exposed `/extract`,
  `/pseudonymize`, `/scan`, `/ingest`, `/ingest/chunks`,
  `/snapshots/create`, `/sync`, `/sync/{repo}`, and `/preview` with no
  application-level auth — only Docker-network isolation. A new
  pure-ASGI `IngestionAuthMiddleware` now validates an
  `Authorization: Bearer <token>` header on every request, with
  `/health` and `/metrics*` exempt. The token lives in the new
  `secrets/ingestion_auth_token.txt` Docker Secret (mirrored at
  `/run/secrets/ingestion_auth_token` and read via
  `shared.config.read_secret`). All callers — mcp-server, pb-proxy,
  pb-worker, pb-demo, pb-seed, and the ingestion service's own
  `/sync` loopback into `_ingest_documents` — pass the token. Token
  comparison uses `hmac.compare_digest` for constant-time evaluation.
  Backward-compatible: when the token is empty (e.g. existing
  deployments mid-upgrade), the middleware logs a loud warning at
  startup and lets requests through, so rolling out the secret is not
  a breaking change. `scripts/quickstart.sh` auto-generates a 32-byte
  hex token alongside the existing secrets.
- **`pb_ingestion_auth_failures_total{reason}`** Prometheus counter
  on the new middleware. Labels `missing` (no/invalid header) and
  `invalid` (wrong token) so operators can distinguish "service down"
  from "stale token" without log grepping.
- **E2E test for chat-path document attachments** ([B-51](docs/BACKLOG.md)).
  New suite at `tests/integration/e2e/test_document_attachment.py`
  covers both block shapes (OpenAI `file` via `/v1/chat/completions`
  and Anthropic `document` via `/v1/messages`) plus the three policy
  error paths (413 oversize, 415 disallowed MIME, 403 viewer denied).
  The "PII pseudonymised before LLM" promise is asserted via
  Prometheus counters
  (`pbproxy_documents_extracted_total{status="ok"}` and
  `pbproxy_pii_entities_pseudonymized_total{entity_type="PERSON"}`),
  which keeps the test robust to LLM-provider availability while still
  proving the pipeline ran in order. Pre-generated fixtures live in
  `testdata/documents/` with a regeneration script for when the corpus
  needs to change.
- **Grafana dashboard panels for document extraction**
  ([B-53](docs/BACKLOG.md)). Four new panels appended to the
  *Powerbrain Overview* dashboard under a *Document Extraction* row:
  proxy doc-extract requests/s by MIME + status
  (`pbproxy_documents_extracted_total`), ingestion `/extract` duration
  p50/p95/p99 (`pb_extract_duration_seconds_bucket`), input-size
  heatmap (`pb_extract_bytes_in_bucket`), and ingestion-side success
  vs. error rate (`pb_extract_requests_total`). All metrics were
  already scraped — this just makes them visible.
- **ADR T-6 — markitdown vs. Docling** ([B-52](docs/BACKLOG.md)).
  Decision recorded in `docs/technology-decisions.md`: stay with
  markitdown by default; ship Docling as an opt-in second backend
  when triggers fire (>20% scanned-PDF corpora, repeat extraction
  errors on tabular PDFs/XLSX, or a layout-fidelity-driven adapter).
  Companion benchmark harness `scripts/benchmark_extractors.py` runs
  both extractors on a directory and prints chars-out + latency for
  each — staying outside the production codepath until the
  benchmark data justifies a backend switch.

### Fixed

- **`pb_verify_audit_chain()` detects inconsistent seeds on an empty
  log** ([#94](https://github.com/nuetzliches/powerbrain/issues/94),
  [#98](https://github.com/nuetzliches/powerbrain/pull/98)).
  Previously returned `valid=true, total_checked=0` even when
  `audit_tail.last_entry_hash` disagreed with the resolved archive
  seed — a state that's a guaranteed chain break on the next insert
  (e.g. genesis reset that forgot to truncate `audit_archive`).
  Migration 023 cross-checks the tail in the empty-range path and now
  returns `valid=false, first_invalid_id=1` proactively. Range-scoped
  calls (`p_start_id > 1`) keep their existing behaviour.
- **`export_audit_log` accepts ISO-8601 datetime strings**
  ([#96](https://github.com/nuetzliches/powerbrain/issues/96),
  [#98](https://github.com/nuetzliches/powerbrain/pull/98)). The
  `since` / `until` parameters were passed to asyncpg as raw strings,
  which fails the TIMESTAMPTZ type check with a confusing 500. New
  `_parse_iso_datetime` helper accepts the `Z` / `+00:00` / naive
  variants, returns a structured 422-style error for malformed input,
  and binds real `datetime` instances. Pattern lifted from the
  existing `validate_pii_access_token()` helper.
- **Audit-chain recovery doc accuracy**
  ([#93](https://github.com/nuetzliches/powerbrain/issues/93),
  [#98](https://github.com/nuetzliches/powerbrain/pull/98)).
  `docs/audit-chain-migration.md` no longer claims
  `pb_audit_checkpoint_and_prune` deletes broken segments (the DELETE
  is fail-closed behind `IF v_verify.valid`); Option B's manual
  TRUNCATE uses a CTE pattern that feeds the archive hash into the
  `UPDATE audit_tail` (the previous SQL referenced a non-existent
  column); the genesis-reset path is documented explicitly with the
  caveat that `audit_archive` must be truncated alongside or the
  next insert breaks at id=1.

## [0.7.1] - 2026-04-22

Three concurrency and misconfiguration bug fixes filed against the
0.7.0 production deployment. Each shipped as an independent PR so it
can be reviewed and reverted separately.

### Fixed

- **`POST /sync` works inside the container again** ([#60](https://github.com/nuetzliches/powerbrain/issues/60),
  [#61](https://github.com/nuetzliches/powerbrain/pull/61)).
  The ingestion Dockerfile flattened `ingestion/*` into `/app/`, so
  `ingestion_api.py`'s deferred `from ingestion.sync_service import …`
  (and ~40 more absolute imports across `sync_service.py` and the
  adapters) could never resolve. Preserved the package layout
  (`COPY ingestion/ /app/ingestion/`), added the missing top-level
  `__init__.py`, set `PYTHONPATH=/app/ingestion:/app` so sibling
  imports (`from pii_scanner import …`) keep working without rewriting
  40+ statements, and switched uvicorn to `ingestion.ingestion_api:app`.
  Side effect: the pre-existing compose mount
  `./ingestion/repos.yaml:/app/ingestion/repos.yaml:ro` now targets a
  real path inside the image.

- **Missing OPA policies surface loudly instead of silently denying**
  ([#59 part 2](https://github.com/nuetzliches/powerbrain/issues/59),
  [#62](https://github.com/nuetzliches/powerbrain/pull/62)).
  Every OPA helper used `resp.json().get("result", {})`, which
  collapsed an OPA response with no `result` field (policy not
  loaded) into `allowed=False, min_score=0.0`. That produced the
  mathematically-impossible rejection log
  `quality_score 0.629 < required 0.000` and hours of debugging on
  fresh deployments.
  - New `shared/opa_client.py` with `opa_query()` that raises
    `OpaPolicyMissingError` when `result` is absent.
  - `verify_required_policies()` runs on service startup —
    ingestion, mcp-server, and pb-proxy refuse to boot if a required
    policy package is missing. Env var `SKIP_OPA_STARTUP_CHECK=true`
    opts out for unit tests.
  - Quality gate now uses `min_score=-1.0` as a sentinel when the
    policy is missing, so the value itself flags a configuration
    issue in logs and the `ingestion_rejections` table.
  - 13 new unit tests for the shared helper, 8 regression tests for
    the ingestion missing-policy path.

- **Audit hash chain stays valid under concurrent writers**
  ([#59 part 1](https://github.com/nuetzliches/powerbrain/issues/59),
  [#63](https://github.com/nuetzliches/powerbrain/pull/63)).
  `audit_integrity.valid` flipped to `false` after ingesting 4 861
  documents with concurrency=8, breaking EU AI Act Art. 12
  tamper-evidence. Two separate root causes:
  1. `pg_advisory_xact_lock` serialized trigger execution but did
     not invalidate the parent INSERT's READ COMMITTED snapshot —
     the waiter's SELECT still read the stale tail hash after the
     lock was released.
  2. `BIGSERIAL` assigned `id` via the column DEFAULT *before* the
     trigger ran, so id order and chain order could diverge under
     concurrency. `pb_verify_audit_chain()` walks by `id ASC` and
     flagged any divergence as a break, even when every individual
     hash was sound.
  - New migration `init-db/022_audit_tail_pointer.sql` introduces a
    single-row `audit_tail` table protected by
    `SELECT … FOR UPDATE`, and derives `NEW.id` from
    `last_entry_id + 1` atomically inside the lock. id order now
    matches chain order by construction.
  - `pb_audit_checkpoint_and_prune()` rewritten to take the same
    tail lock instead of the advisory lock.
  - New integration test: 16 writers × 100 rows = 1 600 concurrent
    inserts → chain `valid=true`.
  - `docs/audit-chain-migration.md` documents the operator
    procedure for deployments whose chain is already broken.

## [0.7.0] - 2026-04-20

One optional precision layer for the PII pipeline, plus the Tab-D
demo reliability fixes that came out of a live debugging session on
CPU Ollama.

### Added

- **Semantic PII Verifier (Option B)** (#56): optional precision layer
  that sits between Presidio's `scan_text` output and the rest of the
  ingestion pipeline. Presidio has excellent recall but over-flags
  German compound nouns (`Zahlungsstatus`, `Geschäftsführer`,
  `Sparkasse Köln`) as PERSON / LOCATION. The verifier catches those
  false positives without touching recall.
  - New abstraction `shared/pii_verify_provider.py` (same factory
    pattern as `rerank_provider.py`). Two backends ship: `noop`
    (community default, pass-through) and `llm` (OpenAI-compatible
    chat, e.g. Ollama / qwen2.5:3b).
  - Pattern types (IBAN, email, phone, DOB) skip the verifier — their
    Presidio score is already trustworthy. Ambiguous types batch into
    a single low-temperature chat call per document with ±60-char
    context windows.
  - **Fail-open** on any error: unreachable LLM, malformed JSON,
    timeout → keep every candidate Presidio generated.
  - OPA-policy-driven backend via
    `pb.config.ingestion.pii_verifier.{enabled,backend,min_confidence_keep}`
    so admins flip runtime behaviour through `manage_policies` without
    restarting ingestion.
  - Prometheus metrics:
    `pb_ingestion_pii_verifier_calls_total{entity_type,backend,result}`
    and `pb_ingestion_pii_verifier_duration_seconds{backend}`.
  - Applied in both the production `ingest_text_chunks` per-chunk
    loop and the `/preview` dry-run, so demo Tab E renders
    `{input, forwarded, reviewed, kept, reverted}` stats live plus a
    `verifier.before` snapshot for contrast.
  - Live verification on the NovaTech SharePoint fixture: 9 raw
    Presidio candidates → 6 after verifier (3 false positives removed)
    in ~12 s on qwen2.5:3b CPU.
  - Docs: `docs/pii-verifier.md` (architecture + configuration) and
    `docs/pii-custom-model.md` (long-horizon roadmap for a fine-tuned
    German PII model — triggers, phases, why we're not building it
    today).
- **Tab-D "Advanced proxy settings" expander** (#57): model, request
  timeout (30–600 s), and `max_tokens` (100–1000) editable per run in
  the sales-demo "MCP vs Proxy" tab. Session-scoped; persistent
  override via `PROXY_MODEL` / `PROXY_TIMEOUT` env on the `pb-demo`
  service. Plus a diagnostic panel that surfaces tool-call count,
  finish_reason, and the typical failure modes ("LLM made no tool
  calls" / "Empty response after N LLM call(s) and M tool call(s)").
- **Demo playbook — Tuning section** (#57): new chapter in
  `docs/playbook-sales-demo.md` covering local-LLM levers (timeout,
  `max_tokens`, Ollama warm-up, `OLLAMA_NUM_CTX`, GPU profile, host
  Ollama, hosted fallback model) with effect estimates.
- **Follow-up plan for separated LLM pools** (#57):
  `docs/plans/2026-04-20-separate-summary-llm-pool.md` specifies the
  clean fix for agent-loop vs summary-LLM contention — a second
  `CompletionProvider` instance in `mcp-server` routed through
  `SUMMARIZATION_PROVIDER_URL` / `SUMMARIZATION_MODEL` /
  `SUMMARIZATION_API_KEY`. Tracked for the next release.

### Changed

- **`CompletionProvider.generate()` accepts per-call `timeout`**
  (#57): shared LLM provider abstraction lets callers override the
  httpx client default when they sit behind a stricter upstream
  deadline (e.g. the summary LLM call behind the proxy's
  `TOOL_CALL_TIMEOUT`). Backward-compatible; existing callers unchanged.
- **MCP server `SUMMARIZATION_TIMEOUT`** (#57): new env var (default
  **15 s**) gates the summary LLM call in `search_knowledge` and
  `get_code_context`. Keeps the graceful-fallback branch ("return raw
  chunks") well below the proxy's `TOOL_CALL_TIMEOUT` so the fallback
  response actually reaches the upstream caller.
- **pb-proxy `TOOL_CALL_TIMEOUT`** (#57): default **30 s → 60 s**.
  Required headroom for the mcp-server's summary attempt +
  raw-chunks fallback + response envelope on CPU Ollama. Lower it
  again when pointing at a hosted provider with sub-second latency.
- **Proxy tool allowlist ships by default** (#57):
  `pb-proxy/mcp_servers.yaml` now declares a `tool_whitelist` with
  five entries (`search_knowledge`, `get_document`, `graph_query`,
  `query_data`, `check_policy`). The MCP server still exposes all 23
  tools — they are just hidden from the LLM by default so small
  local models (qwen2.5:3b) stop suffering choice-paralysis from the
  ~8–10 kB of schema overhead. Enterprise deployments with capable
  models (Haiku, gpt-4o-mini, qwen2.5:14b+) can drop the whitelist.

### Fixed

- **Tab D proxy timeout / empty-response deadlock** (#57): the
  pb-proxy agent loop and the mcp-server's forced summarisation
  (`pb.summarization.summarize_required` for `confidential` hits)
  both hit 30 s httpx deadlines simultaneously, so the graceful
  raw-chunks fallback never reached the proxy. Stale symptom in the
  demo: "Read timed out" or "(empty response)" after ~60 s. The new
  `SUMMARIZATION_TIMEOUT=15` + `TOOL_CALL_TIMEOUT=60` ordering makes
  the fallback land ~45 s before the proxy gives up.
- **Demo client default read timeout** (#57): `_ProxyClient.timeout`
  default **60 s → 180 s** (+ `PROXY_TIMEOUT` env override) so a
  slow-but-successful agent-loop iteration on CPU doesn't get killed
  by the HTTP client in Streamlit before the proxy returns.

### Migration notes

- **`SUMMARIZATION_TIMEOUT`** (new env, default 15 s) and
  **`TOOL_CALL_TIMEOUT`** (default raised from 30 s to 60 s) are
  plumbed through `docker-compose.yml`. No action needed unless you
  override these in a custom compose file — in that case, make sure
  `TOOL_CALL_TIMEOUT > SUMMARIZATION_TIMEOUT` by a comfortable margin.
- **`pb-proxy/mcp_servers.yaml` tool_whitelist**: existing deployments
  that rely on tools outside the five-entry default (e.g. LLM-driven
  `submit_feedback` or `graph_mutate`) must explicitly add them to
  the whitelist or remove the whitelist entirely. The MCP server
  still exposes every tool — only the proxy-side injection is
  narrowed.
- **OPA `pb.config.ingestion.pii_verifier` section** (new): defaults
  to `enabled=false`, `backend=noop`. No behaviour change unless you
  opt in. Flip at runtime via `manage_policies` once an LLM endpoint
  is reachable.

### Stats

- **2 merged PRs** since v0.6.0: #56, #57.
- **+1964 / −21** lines across 19 files (2 new source files, 5 new
  docs/plans files).
- Unit tests: full suite **953 passing** (plus 30 intentionally
  skipped, 8 integration-deselected). New `shared/tests/
  test_pii_verify_provider.py` adds comprehensive coverage for the
  verifier's noop/LLM backends, the skip-by-type logic, and the
  fail-open guarantees.
- OPA tests: unchanged pass count (no new Rego paths, only a new
  `pb.config.ingestion.pii_verifier` data section).

## [0.6.0] - 2026-04-19

Four features that together answer the questions enterprise
decision-makers ask most often — *who can see what*, *what happens
to our PII*, and *what does your pipeline do with our documents* —
plus the sales-demo surfaces that make those answers visible in
fifteen minutes.

### Added

- **Sales-Demo UI** (#50): opt-in Streamlit app `pb-demo` on port 8095,
  profile `demo`. Starts out with three tabs:
  - Tab A *Same question, different answers* — analyst vs viewer
    side-by-side on the same query, shows OPA access matrix in action.
  - Tab B *We never stored the secret* — live PII vault scan → ingest →
    HMAC-token reveal with purpose-bound `fields_to_redact`.
  - Tab C *The org behind the answer* — `streamlit-agraph` rendering of
    the NovaTech knowledge graph (8 employees → 3 departments → 4
    projects) via `graph_query`.
  - Plus pre-seeded demo keys (`pb_demo_analyst_localonly`,
    `pb_demo_viewer_localonly`), 6 German-PII customer records, and an
    8-employee graph seed. Quickstart gained `--seed` / `--demo` flags
    and auto-generates Postgres / HMAC / proxy secrets.
  - 15-min presenter script in `docs/playbook-sales-demo.md`.
- **Editions (Community vs Enterprise) + `/vault/resolve`** (#52):
  - Every service advertises `"edition": "community"` (`mcp-server`)
    or `"enterprise"` (`pb-proxy`) on `/health` + `/transparency`.
  - New mcp-server endpoint `POST /vault/resolve` does text-level
    de-pseudonymisation (regex extract `[ENTITY_TYPE:hash]` → SQL
    hash-match → OPA vault policy → purpose-based field redaction →
    audit log).
  - pb-proxy's agent loop calls `/vault/resolve` on tool results under
    the OPA-gated `pb.proxy.pii_resolve_tool_results` policy (enabled /
    allowed_roles / allowed_purposes / default_purpose). Client
    declares purpose via `X-Purpose` header.
  - Stats surface on `_proxy.vault_resolutions` + `X-Proxy-Vault-*`
    response headers.
  - Demo Tab D *MCP vs Proxy* renders both paths side-by-side on the
    same query; purpose toggle changes what gets redacted.
  - Full capability matrix + topology in `docs/editions.md`.
- **Pipeline Inspector + `/preview` endpoint** (#54):
  - New `POST /preview` on the ingestion service: runs the full
    pipeline (optional extract → PII scan → quality score + OPA
    ingestion gate → OPA privacy decision) against a document
    without persisting to PostgreSQL or Qdrant. Returns a structured
    `{extract, scan, quality, privacy, summary}` payload with
    per-phase timings and an explicit `would_ingest` verdict.
  - Demo Tab E *Pipeline Inspector* with three adapter-representative
    fixtures (SharePoint contract, Outlook support email, GitHub
    README) plus optional file upload. Editable `classification` /
    `source_type` / `legal_basis` so the OPA policy effect is
    visible live.

### Changed

- **OPA policy data path** (#50): `opa-policies/data.json` moved from
  the `pb/` subdirectory to the repo-level `opa-policies/` so the OPA
  `run` loader mounts it at `data.pb.config.*` instead of the
  doubly-prefixed `data.pb.pb.config.*`. Without this, the ingestion
  `pii_action` check was silently stuck on the default `block`. CI
  invocations and Docker volume mounts updated; no deployer action
  needed beyond pulling the new image.
- **Graph PII masking** (#51): `_mask_graph_pii` in the MCP server is
  now deterministic and policy-driven. The previous Presidio-per-value
  scan produced inconsistent results on non-English names (Elena →
  `<PERSON>`, Tim → unchanged, Sarah → `<LOCATION>`). New `pb.config.
  graph_pii_keys` section in `data.json` maps graph property keys to
  Presidio entity-type labels; the walker replaces matched values
  with `<ENTITY_TYPE>` deterministically. Admin-editable at runtime
  via `manage_policies`.
- **Access matrix** (#50): `confidential` now includes `analyst` and
  `developer` in addition to `admin`. Matches realistic RBAC for
  customer records and salary bands; `restricted` stays admin-only.
- **Quickstart flags** (#50): `./scripts/quickstart.sh --seed` seeds
  the 21 base documents; `--demo` adds the PII customer records, the
  graph seed, the demo UI profile, the pb-proxy profile, and pulls the
  summarisation model. Auto-generates Postgres / vault / proxy-service
  tokens — no more manual `.env` editing.

### Fixed

- **PII pseudonymisation overlap** (#53): Presidio can emit overlapping
  hits on the same character range (classic case: the trailing digit
  run of a German IBAN is also a valid phone number). The pseudonymiser
  replaced both in descending-position order and produced nested
  artefacts like `[IBAN_CODE:73c1acb4]db0d4]`. New
  `_resolve_overlapping_spans` helper picks one hit per overlap —
  higher score wins, then longer span, then earlier start — and is
  applied consistently in `scan_text`, `mask_text`, and
  `pseudonymize_text`.
- **Graph `find_path`** (#50): `_parse_return_columns` in
  `graph_service.py` was splitting `"RETURN a, r, b LIMIT 1"` into
  three columns and sanitising the third to `"bLIMIT1"`, which broke
  the row lookup. The parser now strips `LIMIT`/`ORDER BY`/`SKIP`/
  `OFFSET` before splitting.
- **DE date-of-birth recognizer** (#50): dropped the pure-numeric
  `dd.mm.yyyy` pattern because it fired on harmless policy dates (e.g.
  "gültig ab 01.01.2025"), which the ingestion quality gate then
  blocked wholesale. The keyword-anchored variant (`Geburtsdatum:`,
  `geb.`, `geboren am`) remains and covers the real use case.
- **Vault schema width** (#50): `pii_vault.pseudonym_mapping.pseudonym`
  was `VARCHAR(20)` — too narrow for longer entity tags like
  `[DE_DATE_OF_BIRTH:…]` or `[EMAIL_ADDRESS:…]`. Migration `021`
  widens it to `VARCHAR(64)` idempotently.
- **pb-proxy bootstrap** (#52): the proxy service token in
  `secrets/mcp_auth_token.txt` must be a registered `api_keys` row so
  the proxy can reach mcp-server under `AUTH_REQUIRED=true`. The
  quickstart now registers it automatically via
  `scripts/register-proxy-key.sh`; previously this was a silent
  startup failure on fresh installs.
- **Seed authentication** (#50): `testdata/seed.py` now sends
  `Authorization: Bearer` on every MCP call; the graph seed step
  previously couldn't initialise the MCP session under the default
  `AUTH_REQUIRED=true`, and the seed container aborted before the
  graph ran.
- **Suggestion buttons in demo Tab A** (#51): moved outside the
  `st.form` so clicks actually fire and update the query / trigger
  the search.

### Migration notes

- **`init-db/020_viewer_role.sql`** (new): widens the `api_keys.
  agent_role` CHECK to include `viewer` so the pre-seeded demo viewer
  key is valid. Existing deployments pick this up on next restart;
  no manual steps required.
- **`init-db/021_widen_vault_pseudonym.sql`** (new): widens
  `pii_vault.pseudonym_mapping.pseudonym` to `VARCHAR(64)`.
  Idempotent — re-runs on existing databases.
- **`opa-policies/data.json`** moved from `opa-policies/pb/data.json`
  (same for `policy_data_schema.json`). Deployers who mount these
  paths directly in custom `docker-compose` overrides should update
  their mounts.
- **pb-proxy service token**: `secrets/mcp_auth_token.txt` is now
  auto-registered by `quickstart.sh`. Manual deployments should run
  `./scripts/register-proxy-key.sh` once after the Postgres init
  completes.

### Stats

- **989 unit tests** pass (13 new), **68.95% coverage** (CI threshold
  68%).
- **131/131 OPA policy tests** pass (12 new for `graph_pii_keys` +
  `pii_resolve_tool_results` + viewer role regressions).
- **5 merged PRs** since v0.5.0: #50, #51, #52, #53, #54.

## [0.5.0] - 2026-04-17

### Added

- **Office 365 Adapter** (#42): second source adapter. Syncs SharePoint,
  OneDrive, Outlook Mail, Teams Messages, and OneNote into the knowledge
  base via Microsoft Graph API.
  - Delta Queries for incremental sync (all providers except OneNote,
    which uses timestamp-based sync).
  - OAuth2 Client Credentials (app-only) + Delegated Auth (OneNote,
    post-March-2025 Microsoft Graph policy).
  - Content extraction via Microsoft `markitdown` + format-specific
    fallbacks (python-docx, openpyxl, python-pptx, BeautifulSoup).
  - Site-level classification in YAML config.
  - Teams ↔ SharePoint deduplication (file attachments stored as refs only).
  - Resource Unit budget tracking + `$batch` API usage.
  - Config: `ingestion/office365.yaml` (example provided).
- **Shared document extraction** (`ingestion/content_extraction/`, #46) —
  `ContentExtractor` lifted out of the Office 365 adapter into a reusable
  module so all adapters + the `/extract` endpoint share one surface.
- **`POST /extract` endpoint** on the ingestion service (#46) — converts
  base64-encoded binary documents (PDF, DOCX, XLSX, PPTX, MSG, EML, RTF, ...)
  to text. Size-capped via `EXTRACT_MAX_BYTES` (default 25 MB) and bounded
  by `EXTRACT_TIMEOUT_SECONDS` (default 30 s).
- **Chat-path document attachments** in pb-proxy (#46,
  `/v1/chat/completions` and `/v1/messages`) — extracts `file`/`input_file`
  (OpenAI) and `document` (Anthropic) blocks via `/extract` before PII
  scanning and LLM forwarding.
- **GitHub adapter opt-in document ingestion** (#46) via
  `allow_documents: true` in `repos.yaml` — fetches Office/PDF files as
  bytes and runs them through the shared `ContentExtractor`. Ingested as
  `source_type="github-document"`.
- **New OPA policy `pb.proxy.documents`** (#46) — gates chat attachments by
  role, size, MIME type, and per-request file count. Data-driven via
  `data.json`.
- **Optional Tesseract OCR fallback** (#46) for scanned PDFs — activated at
  build time via `--build-arg WITH_OCR=true` plus runtime
  `OCR_FALLBACK_ENABLED=true`.
- **Prometheus metrics**: `pb_extract_requests_total`,
  `pb_extract_duration_seconds`, `pb_extract_bytes_in`,
  `pbproxy_documents_extracted_total`, `pbproxy_documents_extracted_bytes`.
- 42 new unit tests (content_extraction, /extract endpoint, pb-proxy
  document extraction, Anthropic document normalization) + 11 new OPA tests.

### Changed

- `ingestion/adapters/office365/content.py` is now a thin shim re-exporting
  from `ingestion.content_extraction` — fully backward compatible.
- `ingestion/requirements.txt` now hosts markitdown + Office document
  fallbacks (moved up from `office365/requirements.txt`) so all consumers
  (adapters + `/extract` endpoint) share one dependency surface.
- GitHub adapter's `BINARY_EXTENSIONS` split into `HARD_BINARY_EXTENSIONS`
  (images/archives — always blocked) and `DOCUMENT_EXTENSIONS` (opt-in via
  `allow_documents`). Legacy alias preserved for backward compatibility.
- Consolidated dependency version bumps across all services (#47):
  - Security floors: `pyjwt>=2.10`, `pyyaml>=6.0.2`, `httpx>=0.28`,
    `msal>=1.32` (Entra-ID fixes), `litellm>=1.80` (proxy).
  - OpenTelemetry unified across ingestion/pb-proxy/reranker/mcp-server at
    `>=1.27` (core) and `>=0.48b0` (instrumentation).
  - `torch>=2.6` in reranker for CVE coverage.
  - Worker requirements relaxed from hard pins to SemVer-safe ranges
    (`apscheduler>=3.11,<4.0` — 4.x is an incompatible rewrite,
    `python-dotenv>=1.2,<2.0`, `qdrant-client>=1.15,<2.0`, etc.).

### Fixed

- CI PR validation detects changes correctly across multi-commit pushes
  (#43) — use `BEFORE_SHA` instead of `HEAD^` for the change-detection
  baseline.
- `python-pptx<1.0` ceiling excluded the already-released 1.0.x series —
  corrected to `<2.0` (#47).

### Documentation

- GitHub adapter reference added and cross-linked with the Office 365
  adapter docs (#44).
- `docs/architecture.md` §2.9 Document Extraction — new section describing
  the shared extractor, policy gates, and OCR fallback.
- 4 new backlog tickets logged: B-50 (unified ingestion auth layer),
  B-51 (E2E test for chat document attachments), B-52 (ADR markitdown vs.
  Docling), B-53 (Grafana panels for extraction metrics).

## [0.4.0] - 2026-04-10

### Added

- GitHub Adapter: first source adapter for knowledge base ingestion from GitHub repositories (#39)
  - Incremental sync via commit SHA tracking (`repo_sync_state` table)
  - Configurable include/exclude path patterns, default binary/noise skip rules
  - PAT + GitHub App authentication (JWT → installation token)
  - Polling via pb-worker (configurable interval) + `POST /sync/{repo}` endpoint
  - Full pipeline: PII scan, OPA quality gate, embedding, context layers
  - Cascade deletion of removed files (Qdrant, PG, vault, graph)
  - Config: `ingestion/repos.yaml` (example provided)
  - 59 new unit tests, 2 new OPA tests (111 total)

## [0.3.1] - 2026-04-10

### Added

- README badges (CI, License, Docker, MCP) and corrected tool count (16 → 23)
- GitHub Issue Templates (bug report, feature request) and PR template
- SECURITY.md with vulnerability reporting policy
- SUPPORT.md pointing to Discussions, Issues, and Docs
- `scripts/quickstart.sh` for automated first-time setup with optional demo data seeding
- `docs/getting-started.md` — step-by-step tutorial with authentication guide
- `docs/mcp-tools.md` — all 23 MCP tools with parameters and access roles
- `docker-compose.ghcr.yml` — compose override for pre-built GHCR images
- `.github/workflows/release.yml` — automated GHCR image publishing and GitHub Releases on tag push
- `.github/dependabot.yml` — weekly dependency updates for pip, Docker, and GitHub Actions
- `.pre-commit-config.yaml` with ruff linter and formatter
- Locust load test for MCP search pipeline (`tests/load/`)
- CI security scanning: `pip-audit` (dependency vulnerabilities) + `bandit` (static analysis)
- CI coverage threshold enforcement (`--cov-fail-under=73`)

### Changed

- CLAUDE.md updated with new files, CI changes, and expanded pre-public checklist
- Quick Start in README references `quickstart.sh` and includes health verification step
- MCP client config examples now include Bearer token authentication

## [0.3.0] - 2026-04-09

### Added

- PII masking for graph_query/graph_mutate results via ingestion `/scan` endpoint (B-30)
- Metadata PII redaction in search_knowledge/get_code_context based on configurable field mapping and OPA `fields_to_redact` policy (B-31)
- `manage_policies` MCP tool for runtime OPA policy data management with JSON Schema validation (B-12)
- `boost_corrections` reranking parameter for user-corrected documents (B-13)
- OPAL integration for real-time policy sync from git repos (`--profile opal`) (B-10)
- CHANGELOG v0.1.0 and v0.2.0 entries (#8)

### Changed

- PipelineStep fallback in pb-proxy now matches shared/telemetry.py signature including `to_dict()` (B-20)
- BACKLOG.md fully closed out — all items completed or marked won't do

### Fixed

- Missing `pyyaml` dependency in mcp-server/requirements.txt
- EU flag emoji replaced with ⚖️ for cross-platform display in README
- CLAUDE.md: tool count, directory structure, components table, secrets list updated to match reality

## [0.2.0] - 2026-04-09

### Added

- EU AI Act compliance implementation (Art. 9, 11-15): risk management, technical documentation, transparency reporting, human oversight, accuracy/robustness monitoring, and pb-worker background service (#5, #6)

### Changed

- Translate all German comments, docstrings, and documentation to English (#7)

### Fixed

- Correct license reference in README from MIT to Apache 2.0 (#4)

## [0.1.0] - 2026-04-08

Initial public release of the Powerbrain Context Engine.

### Added

- MCP Server with 12 tools (search, query, ingest, graph, policy, classification)
- 3-stage search pipeline: Qdrant vector search, OPA policy filtering, Cross-Encoder reranking
- Configurable reranker backend (Powerbrain/TEI/Cohere) via strategy pattern
- OPA-controlled context summarization with LLM provider abstraction
- Data-driven OPA policies (access, privacy, rules, summarization, proxy) with JSON Schema validation
- Sealed Vault for GDPR-compliant PII pseudonymization (dual storage, HMAC tokens, purpose binding)
- PII Scanner (Microsoft Presidio) with configurable entity types and custom recognizers
- Knowledge Graph via Apache AGE (queries and mutations)
- Context Layers (L0/L1/L2) for progressive document loading
- Knowledge versioning with snapshots
- AI Provider Proxy with multi-MCP-server aggregation, SSE streaming, and per-provider key management
- Proxy authentication (ASGI middleware, pb\_ API keys, identity propagation)
- Docker Secrets support with env var fallback
- Optional TLS via Caddy reverse proxy
- Structured telemetry (OpenTelemetry tracing, Prometheus metrics, Grafana dashboards)
- Performance caches (embedding cache, OPA result cache, batch embedding)
- Evaluation and feedback loop
- Monitoring stack (Prometheus, Grafana, Tempo)
- CI workflows (GitHub Actions + Forgejo for internal use)
- Comprehensive documentation (architecture, deployment, scalability, GDPR, ADRs)

[0.4.0]: https://github.com/nuetzliches/powerbrain/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/nuetzliches/powerbrain/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/nuetzliches/powerbrain/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/nuetzliches/powerbrain/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nuetzliches/powerbrain/releases/tag/v0.1.0
