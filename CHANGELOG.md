# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- CI workflows for Forgejo and GitHub Actions
- Comprehensive documentation (architecture, deployment, scalability, GDPR, ADRs)

[0.2.0]: https://github.com/nuetzliches/powerbrain/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nuetzliches/powerbrain/releases/tag/v0.1.0
