# What is PowerBrain?

PowerBrain is a self-hosted knowledge base designed for AI agent access via the Model Context Protocol (MCP). All components are open source and run as Docker containers. It enforces data classification, GDPR compliance, and policy-driven access control on every request.

## Key Features

- **MCP-Native Access** -- Agents interact exclusively through the Model Context Protocol (10 tools: search, query, ingest, graph, policy checks, and more)
- **3-Stage Search Pipeline** -- Qdrant vector search (oversampled 5x) -> OPA policy filter -> Cross-Encoder reranking for Top-N results
- **Data Classification** -- Every data object has a classification level (public, internal, confidential, restricted) enforced by OPA on every request
- **Sealed Vault (Dual Storage)** -- PII data stored both pseudonymized (in Qdrant for search quality) and as originals (in a secured PostgreSQL vault with RLS), with HMAC-token-based elevated access
- **GDPR Compliance** -- PII scanning (Microsoft Presidio), purpose limitation via OPA policy, retention periods with automatic cleanup, Art. 17 right to erasure (2-tier deletion), audit logging
- **Knowledge Graph** -- Apache AGE on PostgreSQL for entity relationships, path queries, and structured knowledge
- **Policy Engine** -- Open Policy Agent (OPA) with Rego rules for access control, privacy decisions, and business rules
- **Local Embeddings** -- Ollama with nomic-embed-text (768d) for fully self-hosted vector generation
- **Knowledge Versioning** -- Snapshots and version tracking for knowledge base content
- **Monitoring and Observability** -- Prometheus metrics, Grafana dashboards, Tempo distributed tracing
- **Forgejo Integration** -- Policies, schemas, and docs sourced from existing Forgejo git repos (no separate git container needed)

## Problem / Solution

| Problem | How PowerBrain Solves It |
|---|---|
| AI agents need structured access to organizational knowledge | MCP server provides 10 specialized tools (search, query, ingest, graph, policy) as a single access point |
| PII in training/search data creates legal risk (GDPR) | Presidio PII scanner at ingestion, OPA-driven actions (mask/pseudonymize/block), Sealed Vault for reversible pseudonymization |
| Destructive PII masking degrades search quality | Sealed Vault pattern: deterministic pseudonyms preserve sentence structure for better embeddings, originals stored securely for authorized access |
| No way to retrieve original PII when legally required | HMAC-signed short-lived tokens with purpose binding and field-level redaction via OPA policy |
| Data classification enforcement is inconsistent | OPA checks every MCP request against classification levels; policy-as-code, changeable without redeployment |
| GDPR Art. 17 right to erasure is complex | 2-tier deletion: restrict (vault deleted, pseudonyms become irreversible and thus anonymous) or full delete (including Qdrant vectors) |
| Vector search alone has poor precision | 3-stage pipeline: Qdrant oversampling x5, OPA policy filter, Cross-Encoder reranking for Top-N |
| Knowledge exists in isolation without relationships | Apache AGE knowledge graph for entity relationships, path queries, and cross-referencing |
| External AI services create data sovereignty concerns | Fully self-hosted: local embeddings (Ollama), local vector DB (Qdrant), local policy engine (OPA) |
| Audit and compliance requirements | Every access logged (GDPR-conform audit trail), vault access separately audited with token hashes |

## Architecture Overview

The MCP server (FastAPI) is the single entry point for all agent interactions. It orchestrates requests across Qdrant (vector search), PostgreSQL 16 with Apache AGE (structured data, knowledge graph, audit logs, and the Sealed Vault), and OPA (policy evaluation on every request). Embeddings are generated locally via Ollama (nomic-embed-text, 768 dimensions), and a Cross-Encoder reranker service improves search precision after policy filtering. Forgejo provides git-based management of OPA policy bundles, JSON schemas, and documentation through its existing infrastructure -- no additional git container is required.

## Getting Started

See `README.md` for setup instructions and `CLAUDE.md` for the full technical reference.
