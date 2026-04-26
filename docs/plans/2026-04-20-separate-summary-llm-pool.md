# Plan: Separate LLM Pool for Summarization

**Status:** Shipped 2026-04-25 — see CHANGELOG `[Unreleased]`.
**Origin:** Live sales-demo debug session 2026-04-19/20 (Tab D, pb-proxy MCP vs Proxy)

## Problem

`pb-proxy` (agent loop) and `pb-mcp-server` (search_knowledge
summarization) share a single Ollama instance serving a single
`qwen2.5:3b` model. On CPU this produces two pathologies:

1. **Contention.** The agent-loop LLM call and the in-pipeline
   summarization call serialise through Ollama, doubling wall-clock
   time.
2. **Coupled timeouts.** When both paths fire within seconds of each
   other, the summarization call burns the entire `TOOL_CALL_TIMEOUT`
   budget before returning — leading to the 30-s deadlock we fixed with
   [docs/plans/—no file; changes in-place 2026-04-20](.) (Option A):
   `SUMMARIZATION_TIMEOUT=15 s`, `TOOL_CALL_TIMEOUT=60 s`.

Option A stops the deadlock but the shared-pool performance ceiling
remains: end-to-end Tab D takes 20–120 s on CPU and the 3B model
routinely picks the wrong tool (`get_document` instead of
`search_knowledge`).

## Goal

Separate the two LLM consumers so:

- Agent-loop decisions run on a **stronger, tool-calling-capable**
  model (hosted `claude-haiku-4-5` / `gpt-4o-mini`, or a larger local
  model with GPU).
- In-pipeline summarization runs on a **small, fast, distilled** model
  — on a dedicated endpoint — so it never blocks the agent loop and
  stays sub-5-seconds even for long chunks.

## Proposed Design

### Option C1 — Two env vars, two endpoints

Already supported by `shared/llm_provider.py` (OpenAI-compat
abstraction). Add:

- `SUMMARIZATION_PROVIDER_URL` (defaults to `LLM_PROVIDER_URL`)
- `SUMMARIZATION_MODEL` (defaults to `LLM_MODEL`)
- `SUMMARIZATION_API_KEY` (defaults to `LLM_API_KEY`)

Build a separate `CompletionProvider` instance in `mcp-server/server.py`
for summarization. Zero changes for single-endpoint deployments.

**Deployment topologies this enables:**

| Setup | Agent loop | Summarization |
|---|---|---|
| Current (single CPU Ollama) | qwen2.5:3b | qwen2.5:3b (contends) |
| **Sidecar split** | qwen2.5:3b | qwen2.5:1.5b on second Ollama container |
| **Hosted agent** | anthropic/claude-haiku-4-5 | qwen2.5:3b local |
| **Fully hosted** | anthropic/claude-haiku-4-5 | openai/gpt-4o-mini |
| **GPU box** | vLLM qwen 14b | TEI-served FLAN-T5-small |

### Option C2 — Pool routing inside CompletionProvider

Teach `CompletionProvider` to accept a `purpose` ("agent" / "summary")
and dispatch via config. Cleaner API but more code and only one
user (MCP). Probably overkill.

Recommendation: **C1**. Minimal surface.

## Work Breakdown

1. Extend `shared/llm_provider.py` — already works, just new instances.
2. `mcp-server/server.py`:
   - Add `SUMMARIZATION_PROVIDER_URL` / `SUMMARIZATION_MODEL` /
     `SUMMARIZATION_API_KEY` constants (fall back to `LLM_*`).
   - Build a second `CompletionProvider` for summarization.
   - Pass it into `summarize_text`.
3. `docker-compose.yml`:
   - Expose new env vars on `mcp-server` service.
   - Optional: sidecar `ollama-summary` container profile (`--profile summary-llm`)
     preloaded with `qwen2.5:1.5b` or similar distilled model.
4. `.env.example`:
   - Document the three new knobs + provide example for the hosted setup.
5. `docs/playbook-sales-demo.md`:
   - Update the "Tuning the local LLM" section to mark Option C as
     shipped.
   - Add deployment-topology table above.
6. `docs/architecture.md`:
   - Add a note in the *LLM Provider Abstraction* section that
     summarization can route to a separate pool.
7. Tests:
   - Unit test that `summarize_text` uses its own provider instance
     when configured.
   - Integration smoke test (gated behind `RUN_INTEGRATION_TESTS=1`):
     two-endpoint mode works end-to-end.

## Out of Scope

- UI settings in Tab D for the summarization endpoint. Admin-level
  concern, belongs in env/compose.
- Quality benchmark of the small summary model. Track separately.
- Migration of embedding provider — already has its own endpoint.

## Success Criteria

With `qwen2.5:3b` in the agent loop and any sidecar for summarization:

- Tab D "Fasse die Daten zu Julia Weber zusammen" completes in under 30 s
  on CPU (currently 20–120 s, highly variable).
- No `Summarization failed, returning raw chunks` warnings under
  normal load — summary completes within its own dedicated budget.
- Agent-loop LLM and summary LLM never share an Ollama slot.

## Context Links

- Option A (applied 2026-04-20): [shared/llm_provider.py](../../shared/llm_provider.py) `generate(timeout=)`,
  [mcp-server/server.py:91](../../mcp-server/server.py:91) `SUMMARIZATION_TIMEOUT`,
  [pb-proxy/config.py:32](../../pb-proxy/config.py:32) `TOOL_CALL_TIMEOUT=60`.
- Tool-allowlist Option (applied 2026-04-20): [pb-proxy/mcp_servers.yaml](../../pb-proxy/mcp_servers.yaml)
  reduces injected tools from 23 → 5.
- Related doc: [docs/playbook-sales-demo.md](../playbook-sales-demo.md) → *Tuning the local LLM*.
