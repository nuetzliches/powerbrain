# Compliance with Claude Desktop / Claude Code subscriptions

This document is for compliance officers, data protection officers, and
architects who need a precise answer to one question: *"If our staff
uses Claude Desktop or Claude Code on a Pro / Max subscription, does
Powerbrain still keep us GDPR- and EU-AI-Act-compliant?"*

The honest answer has nuance. This page lays it out without marketing.

## TL;DR

- Powerbrain **fully governs** what flows through its ingest adapters
  and its MCP tool calls — in both Community and Enterprise editions.
- Powerbrain **only governs the free chat content** when the client
  speaks the proxy on port 8090. Subscription-based Anthropic clients
  (Pro/Max, both Desktop App and `claude /login` in Claude Code)
  **cannot** be redirected to the proxy; their chat channel bypasses
  Powerbrain by design.
- Therefore: if regulated personal data may appear in free chat
  prompts, you need *either* commercial Anthropic plans + Powerbrain
  proxy on the wire, *or* organisational/DLP controls that prevent
  regulated data from reaching subscription chats, *or* a defence-in-
  depth chat-history ingest workflow (see Tier 2 below).
- A Team/Enterprise DPA does **not** automatically discharge EU AI Act
  Articles 12–15 — those remain deployer obligations regardless of the
  Anthropic plan.

## The bypass, precisely

Claude Pro and Claude Max are **consumer plans**. They authenticate via
OAuth against `claude.ai`. Anthropic deliberately does not allow these
plans to redirect the backend endpoint:

| Client | Auth | `ANTHROPIC_BASE_URL` honoured? | Chat content visible to Powerbrain? |
|---|---|---|---|
| Claude Desktop App (Pro/Max) | OAuth | ❌ | ❌ |
| Claude Code (Pro/Max via `claude /login`) | OAuth | ❌ | ❌ (but `UserPromptSubmit` hooks can intercept locally) |
| Claude Code (API key `sk-ant-...`) | API key | ✅ | ✅ via pb-proxy |
| OpenAI-compatible clients (Cursor, OpenCode, …) | API key | ✅ (their own equivalent) | ✅ via pb-proxy |
| Anthropic SDK / API direct | API key | ✅ | ✅ via pb-proxy |

The bypass is not a Powerbrain limitation — it is structural in how
Anthropic packages subscriptions.

## What runs through Powerbrain regardless of the client

These do **not** depend on which client the user runs:

- **Ingest:** GitHub/Office 365/Git adapters authenticate against
  source systems with their own tokens. No Anthropic involvement.
  Pipeline always runs: chunking → Presidio → quality gate → vault
  mapping → embedding.
- **Tool calls:** When the agent calls `search_knowledge`,
  `query_data`, `graph_query`, etc. via the MCP endpoint, OPA checks
  every request. The audit hash chain records every access. Vault
  tokens are still required for PII reveal.

So even a Claude Desktop Pro user, configured with Powerbrain as an
MCP server, gets policy-checked retrieval. What that user *types in
the chat box*, and what Anthropic streams back, remain outside our
perimeter.

## The three-tier mitigation model

Pick the strongest tier you can enforce — they stack.

### Tier 1 — Real-time chat-path protection (preventive, gold standard)

Enforce that any client touching regulated data speaks the proxy:

- Claude Code CLI with `ANTHROPIC_API_KEY` and
  `ANTHROPIC_BASE_URL=http://pb-proxy:8090`.
- OpenAI-compatible clients pointed at `pb-proxy/v1/chat/completions`.
- For Anthropic-format clients: `pb-proxy/v1/messages` (works with
  Claude Code, custom Anthropic-SDK applications, etc.).

Result: PII pseudonymisation happens **before** the LLM sees the
prompt; vault resolution happens **after** the tool call, gated by
OPA `pb.proxy.pii_resolve_tool_results` and the `X-Purpose` header.
GDPR Art. 5, 32 and EU AI Act Art. 12, 13 are addressed for the chat
path itself.

This tier requires an Anthropic **commercial** plan (API, Team or
Enterprise) for the LLM key, because consumer subscriptions cannot
provide an `sk-ant-…` key usable in API mode. Alternatively: a local
LLM (Ollama, vLLM on-prem) routed through the same proxy — no
Anthropic involvement at all.

### Tier 2 — Defence-in-depth chat-history ingest (detective)

For users you genuinely cannot move off Pro/Max (e.g. legacy
workflows, external collaborators with their own subscriptions), pull
their conversation history into Powerbrain on a regular schedule:

- **Option A — Anthropic conversation export** (ToS-clean, official):
  users export their conversations from claude.ai → upload to a
  Powerbrain endpoint (planned: `claude_export_adapter`). Ingestion
  pipeline runs Presidio + quality gate + vault mapping retroactively.
- **Option B — Claude Code local session logs** (ToS-clean): Claude
  Code stores conversation logs locally in
  `~/.claude/projects/<hash>/*.jsonl`. A scheduled adapter parses and
  ingests these. Available regardless of subscription type.
- **Option C — Claude Desktop App local storage** (ToS grey area, brittle
  across app updates): app stores conversations in OS-local IndexedDB
  / SQLite. Not recommended unless the other options are unavailable.

This tier delivers:
- ✅ Audit trail (EU AI Act Art. 12) — retroactive, T+24h
- ✅ Transparency (Art. 13) — user can see what they sent
- ✅ DLP retrospective — flag conversations that contained PII
- ❌ Art. 32 (pseudonymisation *before* processing) — not solvable
  this way; Anthropic still received the cleartext
- ❌ Art. 5 (data minimisation) — same
- ❌ Right-to-erasure end-to-end — Powerbrain can delete its vault
  mapping, but Anthropic's copy is on a separate retention clock

Tier 2 turns *uncontrolled* into *detective with latency*. That is
substantively better than nothing for GDPR Art. 5(2) accountability
("we knew, we logged, we acted") but not equivalent to Tier 1.

### Tier 3 — Endpoint DLP (preventive, organisation-wide)

Tools like Microsoft Purview, Symantec DLP, Netskope, Zscaler can
block or scan HTTPS traffic to `claude.ai` and `*.anthropic.com`
based on classification labels and content patterns.

- ✅ Covers app *and* web
- ✅ Real-time block-before-send
- ❌ Not Powerbrain-native — separate tool, separate procurement
- ❌ Usually block-only or warn-only, not pseudonymise-then-forward
  (DLP cannot reconstruct a valid Anthropic conversation)

Use Tier 3 as the safety net under Tier 1: "regulated data may only
leave the device via the Powerbrain proxy; everything else is
blocked."

## Anthropic plan vs DPA vs EU AI Act

A frequent confusion: *"We have a Claude Team subscription, so we have
a DPA, so we're compliant."* Not quite.

| Plan | DPA inclusion | Training default | Notes |
|---|---|---|---|
| Free / Pro / Max | ❌ No DPA — consumer terms only | Opt-in per user, model trained on accepted chats | No commercial AVV; per-user privacy setting decides training use |
| Team / Enterprise / API / Claude Code commercial | ✅ DPA + SCCs auto-incorporated in Commercial ToS | Not used for training without active opt-in | Signed copy on request via Anthropic Sales |
| Via AWS Bedrock / GCP Vertex | DPA of the hyperscaler | Per hyperscaler | EU residency available via these paths |

**DPA solves GDPR Art. 28** (Auftragsverarbeitung). It does **not**
solve EU AI Act obligations:

- Anthropic has announced intent to sign the **EU GPAI Code of
  Practice**, publishes Model Cards and a Transparency Hub. They cover
  the GPAI-provider duties for Art. 53 GPAI.
- Anthropic does **not** publish a public article-by-article mapping
  to AI Act Art. 12 (logging), Art. 13 (transparency to deployers in
  detail), Art. 14 (human oversight), Art. 15 (accuracy/robustness
  monitoring in the deployment context). The deployer obligations
  remain with **you**, regardless of which Anthropic plan you hold.

Powerbrain provides the deployer-side building blocks for those
articles: audit hash chain (Art. 12), `get_system_info` and
`generate_compliance_doc` (Art. 13, Annex IV), `pending_reviews` and
circuit breaker (Art. 14), drift detection (Art. 15). These are
useful regardless of the Anthropic plan — and necessary regardless of
the Anthropic plan.

## Realistic recommendations by scenario

| Scenario | Recommendation |
|---|---|
| Regulated data (HR, health, finance, customer PII) appears in chat | Tier 1 mandatory. Tier 3 as enforcement. Pro/Max subscriptions disallowed for regulated workflows by policy. |
| Internal R&D, no customer data, no employee data | Pro/Max subscriptions acceptable. Register Powerbrain MCP for retrieval. Document the chat-channel boundary in your DPIA. |
| External collaborators with their own subscriptions | Tier 2 ingest of their exported conversations + AVV clause that requires retroactive export to your Powerbrain. |
| Air-gapped or strict-residency requirement | Tier 1 with local LLM (Ollama/vLLM) via pb-proxy. No Anthropic involvement. |

## What Powerbrain does **not** claim

To be explicit (this matters for an audit):

- Powerbrain does **not** intercept Pro/Max OAuth sessions, does
  **not** proxy claude.ai, and does **not** sign on behalf of
  Anthropic. The Pro/Max bypass is a structural Anthropic decision,
  not a Powerbrain limitation we plan to remove.
- Powerbrain does **not** make a deployer GDPR- or AI-Act-compliant by
  installation. It provides building blocks; the deployer must apply
  them to the actual data flows.
- Powerbrain does **not** replace organisational measures (training,
  AVV with sub-processors, DPIA, breach response). It augments them.

## What to ask Anthropic / your DPO

When the conversation moves beyond what Powerbrain can answer:

1. *"For our seat plan, is the DPA already in effect or do we need
   to sign a separate copy?"* — usually no separate signature on
   commercial plans, but ask for the executed copy in writing.
2. *"What is the default retention on our plan, and can we negotiate
   Zero Data Retention (ZDR)?"* — ZDR is Enterprise/API only, via
   Sales, not self-service. Enterprise default retention is
   unlimited; configurable down to 30 days minimum.
3. *"Where is our data processed? Is EU residency available
   contractually?"* — direct Anthropic does not advertise EU
   residency; AWS Bedrock and GCP Vertex provide EU regions.
4. *"What is your sub-processor list and how is it updated?"* —
   available via Trust Center (login required).

## See also

- [editions.md](editions.md#edition-boundary-what-runs-through-powerbrain--and-what-doesnt) — the Community vs Enterprise data-path matrix
- [gdpr-external-ai-services.md](gdpr-external-ai-services.md) — the underlying GDPR analysis (Art. 28, 32, 44–49, Schrems II)
- [risk-management.md](risk-management.md) — EU AI Act Art. 9 risk register
- [architecture.md](architecture.md) — system-level overview
- [init-db/006_privacy_incidents.sql](../init-db/006_privacy_incidents.sql) — incident-tracking schema (the Powerbrain side of Art. 33/34 readiness)
