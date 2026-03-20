# Search-First MVP Design

## Context

The project already has a clear target architecture for a self-hosted knowledge base with MCP, Qdrant, PostgreSQL, OPA, Ollama, and an optional reranker. The current gap is not missing architecture but missing integration quality: several documented P0 issues block a minimal end-to-end search path from running in Docker.

The approved focus for the first implementation phase is a search-first MVP. The goal is to make the smallest useful slice of the system run reliably before broadening scope to ingestion, snapshots, auth, or full hardening.

## Goal

Deliver a Docker-based MVP where:

1. the required services start successfully,
2. the MCP server is reachable over the network,
3. local OPA policies are loaded and enforced,
4. a real search request flows through the system, and
5. the search path still works when the reranker is unavailable.

## Scope

### In scope

- Fix the MCP transport so the server is reachable from outside the container.
- Repair `mcp-server` image packaging so runtime imports succeed.
- Update Compose so OPA loads the local Rego policies.
- Keep the search path `MCP -> Ollama -> Qdrant -> OPA -> optional reranker` working end to end.
- Define a small reproducible seed or demo-data path so a real search request can be verified.
- Add a small smoke-test strategy for startup, policy loading, MCP reachability, search success, and reranker fallback.

### Out of scope

- Full authentication and authorization hardening.
- Full ingestion API implementation.
- Snapshot workflows.
- SQL and Cypher hardening outside the MVP-critical search path.
- Broad platform work unrelated to making search reachable and testable.

## Recommended Approach

Use a narrow search-first repair strategy.

This approach fixes only the blockers that prevent the search path from running. It avoids turning phase 1 into a full platform rewrite. The search pipeline already exists in code, so the right move is to make that path reachable, package-complete, and verifiable rather than expanding functionality.

Alternative approaches considered but not chosen:

- Platform-first stabilization: more robust, but too slow to first usable outcome.
- Security-first MVP: valuable, but wider than necessary for the chosen goal.

## Target Architecture For Phase 1

The MVP keeps the existing architecture but narrows the required path:

1. A client connects to the MCP server through a network-capable MCP transport.
2. The MCP server creates embeddings through Ollama.
3. The MCP server queries Qdrant.
4. Each candidate hit is checked against OPA.
5. Allowed hits are optionally reranked.
6. The MCP server returns a structured search response.

### Service expectations

- `mcp-server` is the only entry point for the search workflow.
- `opa`, `qdrant`, and `ollama` are hard dependencies for the MVP search path.
- `reranker` is a soft dependency; failure must degrade result quality, not availability.
- `ingestion` is not a core runtime dependency for phase 1 unless it is reused only to seed demo data.

## Implementation Boundaries

### MCP transport

The current `stdio` startup model is unsuitable for Docker-network clients. The MVP must replace it with a network-capable MCP transport and keep Prometheus metrics separated so metrics do not interfere with the MCP endpoint.

### Packaging and Compose

The `mcp-server` image must include all Python modules required at runtime. Compose must mount or load the local OPA policies so the policy engine is not empty at startup.

### Search path

The existing search flow remains the primary feature. Changes should be minimal and should not widen phase 1 into nonessential tool work.

### Data for verification

The MVP needs a known way to verify search. If real data is not guaranteed locally, the implementation must include a tiny reproducible seed path or documented demo fixture.

## Error Handling Rules For The MVP

### Hard failures

These fail the MVP:

- MCP endpoint unreachable
- OPA policies not loaded
- Qdrant unavailable
- Ollama unavailable
- broken container images or runtime imports

### Soft failures

These do not fail the MVP if fallback works:

- reranker unavailable
- monitoring or tracing disabled

### Expected outcomes that are not errors

- empty search results from a valid request
- policy-based denials

## Test Strategy

The first phase should use a small set of smoke tests instead of broad coverage.

### Required checks

1. Compose startup for the minimal service set.
2. OPA policy-loading verification.
3. MCP endpoint reachability over the network.
4. One real end-to-end search request.
5. Reranker-fallback verification.

### Success criteria

The MVP is complete when `docker compose up` can bring up the minimal stack and a real search request returns a technically valid response through the MCP endpoint with policy filtering applied.

## Follow-Up Work After Phase 1

The following items remain important but intentionally deferred:

- authentication and trusted identity propagation
- query hardening for `query_data` and graph mutations
- complete ingestion API and snapshot flows
- broader automated testing
- startup health improvements and connection warmup

## Implementation Order

1. Repair MCP transport.
2. Repair `mcp-server` image and Docker Compose integration.
3. Ensure reproducible search data exists.
4. Add smoke-test and verification steps.
5. Document deferred security and platform work for phase 2.
