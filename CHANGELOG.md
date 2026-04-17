# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Shared document-extraction package (`ingestion/content_extraction/`) — lifts the
  Office 365 adapter's `ContentExtractor` into a reusable module with markitdown
  as the primary backend and python-docx/openpyxl/python-pptx/BeautifulSoup fallbacks.
- `POST /extract` endpoint on the ingestion service — converts base64-encoded
  binary documents (PDF, DOCX, XLSX, PPTX, MSG, EML, RTF, ...) to text.
  Size-capped via `EXTRACT_MAX_BYTES` (default 25 MB) and timeout via
  `EXTRACT_TIMEOUT_SECONDS` (default 30 s).
- Chat-path document attachments in pb-proxy (`/v1/chat/completions` and
  `/v1/messages`) — extracts `file`/`input_file` (OpenAI) and `document`
  (Anthropic) blocks via `/extract` before PII scanning and LLM forwarding.
- GitHub adapter opt-in document ingestion via `allow_documents: true` in
  `repos.yaml` — fetches Office/PDF files as bytes and runs them through the
  shared `ContentExtractor`. Ingested as `source_type="github-document"`.
- New OPA policy `pb.proxy.documents` — gates chat attachments by role, size,
  MIME type, and per-request file count. Data-driven via `data.json`.
- Optional Tesseract OCR fallback for scanned PDFs — activated at build time
  via `--build-arg WITH_OCR=true` plus runtime `OCR_FALLBACK_ENABLED=true`.
- Prometheus metrics: `pb_extract_requests_total`, `pb_extract_duration_seconds`,
  `pb_extract_bytes_in`, `pbproxy_documents_extracted_total`,
  `pbproxy_documents_extracted_bytes`.
- 42 new unit tests (content_extraction, /extract endpoint, pb-proxy document
  extraction, Anthropic document normalization) + 11 new OPA tests.

### Changed

- `ingestion/adapters/office365/content.py` is now a thin shim re-exporting
  from `ingestion.content_extraction` — fully backward compatible.
- `ingestion/requirements.txt` now hosts markitdown + Office document
  fallbacks (moved up from `office365/requirements.txt`) so all consumers
  (adapters + `/extract` endpoint) share one dependency surface.
- GitHub adapter's `BINARY_EXTENSIONS` split into `HARD_BINARY_EXTENSIONS`
  (images/archives — always blocked) and `DOCUMENT_EXTENSIONS` (opt-in via
  `allow_documents`). Legacy alias preserved for backward compatibility.
- Consolidated dependency version bumps across all services:
  - Security floors: `pyjwt>=2.10`, `pyyaml>=6.0.2`, `httpx>=0.28`,
    `msal>=1.32` (Entra-ID fixes), `litellm>=1.80` (proxy).
  - OpenTelemetry unified across ingestion/pb-proxy/reranker/mcp-server at
    `>=1.27` (core) and `>=0.48b0` (instrumentation).
  - `torch>=2.6` in reranker for CVE coverage.
  - `python-pptx` upper bound corrected from `<1.0` to `<2.0` so the current
    1.0.x series installs.
  - Worker requirements relaxed from hard pins to SemVer-safe ranges
    (`apscheduler>=3.11,<4.0` — 4.x is an incompatible rewrite,
    `python-dotenv>=1.2,<2.0`, `qdrant-client>=1.15,<2.0`, etc.).

### Fixed

- `python-pptx<1.0` ceiling excluded the already-released 1.0.x series —
  corrected to `<2.0`.

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
