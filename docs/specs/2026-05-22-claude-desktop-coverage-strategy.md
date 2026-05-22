# Claude Desktop Coverage Strategy — Spec

**Status:** Draft 2026-05-22, not yet approved
**Owner:** Powerbrain core team
**Supersedes:** [PR #174 — Pseudonym Bridge spec, closed 2026-05-22](https://github.com/nuetzliches/powerbrain/pull/174)
**Related:** [docs/editions.md](../editions.md), [docs/compliance-claude-desktop.md](../compliance-claude-desktop.md), [docs/plans/2026-05-20-claude-chat-audit-mirror.md](../plans/2026-05-20-claude-chat-audit-mirror.md)

## Motivation

Enterprise customers want to keep using **Claude Desktop on Pro/Max subscriptions** with Powerbrain mediating as much of the data flow as possible for GDPR / EU AI Act compliance. Three constraints are non-negotiable from the customer side:

1. **Pro/Max stays.** Per-developer Pro/Max is the cost model that fits org budgets. API-priced usage at scale (heavy coding-agent workloads, hundreds of devs) is prohibitively expensive — often 5–20× the subscription cost.
2. **Claude Desktop stays.** Especially the Code tab — the integrated coding-agent UX with sessions, Git isolation, terminal, computer use, and visual diff review is what the org has standardized on. Replacing it with a CLI or a different tool is organisationally not feasible.
3. **"Just ban it" is not viable.** Without an officially supported path, employees use Claude Desktop anyway via shadow IT. Compliance then has zero visibility — worse than a partial-coverage sanctioned solution.

The customer ask is therefore: *what is the maximum honest coverage Powerbrain can deliver for Claude Desktop on Pro/Max, given Anthropic's 2026 policy posture, and what can compliance teams credibly tell their DPO and DPA based on that coverage?*

This spec is the honest answer. It supersedes the Pseudonym Bridge spec, which targeted the wrong abstraction (claude.ai chat webview) and the wrong threat model.

## What changed in 2026 that closes the obvious paths

Three Anthropic policy / enforcement moves reshape the design space:

1. **2026-02 — "Authentication and Credential Use" policy.** OAuth credentials (Free / Pro / Max) are *"intended exclusively for Claude Code and claude.ai"*. Third-party clients using Pro/Max OAuth are now explicitly policy-violating.
2. **2026-04-04 — Third-party harness enforcement.** Anthropic technically blocked third-party harnesses from using Max subscription limits. Projects in the [CLIProxyAPI](https://rogs.me/2026/02/use-your-claude-max-subscription-as-an-api-with-cliproxyapi/) / [Claw Code](https://claw-code.codes/) class were the direct target.
3. **2025-10-08 Consumer Terms Section 3** remains: prohibits reverse-engineering, decompilation, and access to the Service through "automated or non-human means" except via Anthropic API key.

What these close, definitively:

- Wrapping or modifying the Claude Desktop binary
- Forking Claude Code and using Pro/Max OAuth in the fork
- Building a proxy that relays Pro/Max OAuth credentials to api.claude.ai
- DOM-injecting into Anthropic's proprietary client UI

What they do **not** close:

- Powerbrain components running alongside Claude Desktop, used voluntarily by the user
- Powerbrain MCP servers registered through Claude Desktop's official Connectors mechanism
- Network-level controls applied by the customer's IT department to their own endpoints
- Detective audit of conversations after the fact, from data the user already has access to

This spec lives entirely in the "do not close" half.

## Claude Desktop in May 2026 — three tabs, three flow profiles

Claude Desktop (macOS + Windows; Linux still uses the CLI) has three tabs:

| Tab | Purpose | Primary data flow | Tool calls | File access | Computer use |
|---|---|---|---|---|---|
| **Chat** | Free-form conversation | Prompt → LLM → Response | Limited (search etc.) | None | No |
| **Cowork** | Dispatched / longer agentic work | Task → background agent → report | Moderate | Workspace-scoped | No |
| **Code** | Software development (coding agent) | Prompt → agent loop → file edits, terminal, computer use | Extensive | Full project access | Yes (research preview) |

All three authenticate via OAuth against Anthropic. All three send chat content directly to Anthropic's edge. Powerbrain currently sits in two places:

- As a **Connector** (MCP server) registered in Claude Desktop's settings — touches the tool-call layer
- As an **ingestion sink** for content the user explicitly imports via Powerbrain adapters (GitHub, Office 365, etc.) — touches the knowledge-base layer

Everything else flows past Powerbrain.

## Coverage matrix

What each Powerbrain mechanism covers, per Claude Desktop data path:

| Data path | MCP Connector (existing) | Audit Mirror (planned) | Endpoint DLP (customer IT) | Pre-flight (new, this spec) |
|---|---|---|---|---|
| **User prompt** (text typed by user) | ❌ Bypass | ✅ Post-hoc record | ⚠️ Allow/block only, no content inspection | ✅ Pre-submit pseudonymisation, user-driven |
| **LLM response** | ❌ Bypass | ✅ Post-hoc record | ❌ | ✅ Post-receive resolution, user-driven |
| **File attachments** uploaded into Claude | ❌ Bypass | ✅ Mirrored as ref | ⚠️ Block by MIME possible | ✅ Pre-upload scan + pseudonymise |
| **Tool calls** to Powerbrain MCP | ✅ Full OPA + audit | ✅ Identical | n/a | n/a |
| **Tool calls** to other connectors | depends on connector | ✅ Mirrored as event | ⚠️ Network-level | n/a |
| **File edits** by Code tab on user filesystem | n/a (local) | ✅ Diff mirrored | ⚠️ Filesystem ACL | n/a |
| **Terminal commands** in Code tab | n/a (local) | ✅ Command + output mirrored | ⚠️ Process ACL | n/a |
| **Computer use** (mouse/keyboard control) | n/a (local) | ✅ Action log mirrored | ⚠️ Accessibility-API gating | n/a |

The matrix shows the honest picture: **no single Powerbrain mechanism covers everything; the combination of MCP + audit-mirror + endpoint-DLP + pre-flight closes most of the gap most of the time, without ever requiring us to intercept Claude Desktop itself.**

## Components

### A. MCP Connector (existing — formalise as part of strategy)

Powerbrain's `mcp-server` registered as a Connector in Claude Desktop's settings. Provides 28 tools to the agent in any tab. Already covers:

- OPA policy enforcement per tool call
- Vault-token-based PII resolution for tool results
- Audit chain on `agent_access_log`
- Knowledge graph, search, document retrieval, incident reporting

**Status:** Production today. Documented in [docs/mcp-tools.md](../mcp-tools.md) and the Connectors capability row in [docs/editions.md](../editions.md).

**What changes in this spec:** None to the component itself; the spec formalises that the MCP Connector is the **primary coverage layer** and frames the other components as defence-in-depth around it.

### B. Audit Mirror (planned — see [chat-audit-mirror plan](../plans/2026-05-20-claude-chat-audit-mirror.md))

Reads Claude Desktop's local conversation storage (SQLite under `~/Library/Application Support/Claude/...` on macOS, `%AppData%\Claude\...` on Windows), mirrors conversations into Powerbrain's audit chain after the fact.

Coverage:

- Every prompt and response across all three tabs
- File-edit diffs and terminal-command events from the Code tab (Claude Desktop's session log captures these)
- Tool-call invocations (visible in session log even when the connector wasn't Powerbrain)

What it provides:

- GDPR Art. 5 (lawfulness, accountability) — record of every PII-touching interaction
- GDPR Art. 30 (records of processing) — auditable per-user log
- EU AI Act Art. 12 (record keeping) — interactions with high-risk AI system
- Incident response (Art. 33/34) — forensic trace when something leaks

What it does **not** provide:

- Prevention — PII has already reached Anthropic by the time we see it
- Real-time blocking
- Cross-device visibility (mirror runs per host; centralised aggregation is a separate concern)

**Status:** Plan exists, implementation not started. This spec promotes it to the "coverage strategy backbone."

### C. Endpoint DLP (customer-IT deployment pattern)

Network-level controls on the user's endpoint. Customer IT can:

- Allow `api.claude.ai` only for specific roles / classified workstations
- Block uploads above N bytes via egress filtering (limits attachment exfiltration risk)
- Require VPN-tunnelled connection so traffic is at least visible at the boundary (though TLS hides content)
- Block `api.claude.ai` entirely on workstations handling restricted data, falling back to API-keyed Powerbrain proxy

This is **not a Powerbrain component**; Powerbrain documents the pattern and provides recommended firewall / proxy rules.

**Status:** Pattern only. Spec adds a recommendations section in [docs/deployment.md](../deployment.md) with concrete configs for common DLP suites.

### D. Pre-flight (new component — this spec's only new build)

A small client-side application that helps users **pseudonymise text before they paste it into Claude Desktop** and **resolve pseudonyms in Claude's responses after they paste them back**. Critically: Pre-flight does **not** interact with Claude Desktop itself in any way — no DOM injection, no clipboard hooks into Claude's window, no automation. It is a separate tool the user voluntarily invokes.

#### UX

Three invocation patterns, all opt-in:

1. **Global hotkey + popup** (default) — `Ctrl/Cmd+Shift+P` opens a small Pre-flight window. User pastes text → sees pseudonymised version → copies it → switches to Claude Desktop → pastes it. Same flow in reverse for responses.
2. **Clipboard offer** (opt-in) — when Pre-flight is running and the user copies text, a notification offers "Pseudonymise before paste?" Click accept → clipboard is replaced. User pastes into Claude. Skip → clipboard unchanged.
3. **Browser-extension hand-off** — for the rare user that also wants chat-content-side governance on `claude.ai` (the website, not the desktop app), Pre-flight provides hooks the extension uses. Out of scope as a deliverable here; covered in [docs/editions.md](../editions.md) chat-content path.

Pre-flight UI shows:

- Original text (input)
- Pseudonymised text (output, copyable)
- Detected PII categories and counts ("3 person names, 1 email, 1 IBAN")
- Resolution mode for responses: paste a Claude response containing `[TYPE:hash]` tokens → see resolved text alongside

#### Architecture

| Aspect | Choice | Rationale |
|---|---|---|
| Framework | Tauri (Rust backend + system webview UI) | Small footprint (~5MB), no Chromium bundling, OS-keychain integration via `tauri-plugin-keychain` |
| Backend integration | Existing `/pseudonymize` (ingestion) + `/vault/resolve` (mcp-server) | Zero new backend services |
| Auth | Local `pb_` key in OS keychain | Same key model as the rest of Powerbrain; rotatable |
| Offline | No — Pre-flight requires Powerbrain reachable | Acceptable: corporate deploy, Powerbrain on intranet |
| Distribution | Signed installers (macOS notarised, Windows code-signed), org-wide MDM-deployable | Compliance customers run MDM; this is friction-free |
| Branding | "Powerbrain Pre-flight" — explicit, not pretending to be anything else | ToS clarity; user knows they're using a Powerbrain tool |

#### Why this is ToS-clean

- Does not interact with Claude Desktop's process, binary, DOM, or network traffic
- Does not use Anthropic API or Anthropic OAuth credentials
- Is just a clipboard / text utility that calls Powerbrain endpoints
- User explicitly invokes it; nothing automated against Anthropic

The pre-flight pattern is the same as a desktop password manager that helps you generate strong passwords before pasting them into a website. The website never knows the password manager exists.

#### Limitations

- **User must remember to use it.** Pure UX problem. Mitigated by hotkey + clipboard-offer ergonomics + training.
- **Doesn't catch in-app file uploads.** When the user drags a file into Claude Desktop directly, Pre-flight isn't involved. Mitigation: Pre-flight has a "scan file" mode that pseudonymises a file's text content to a clipboard-ready blob; user pastes that instead of dragging the file. Awkward but defensible.
- **Responses stay pseudonymised in Claude's session.** Anthropic stores Claude's response text containing `[PERSON:abc]` tokens. The audit-mirror picks this up correctly. If the user wants resolved text, they paste-back through Pre-flight.
- **Code-tab file edits.** When Claude edits a file in the user's project, the file content is whatever Claude produces. If the user pseudonymised the original prompt with PII tokens, Claude may write code that references `[PERSON:abc]` literally. Pre-flight provides a "resolve file" command that walks a file (or a project directory) and replaces pseudonyms.

These limitations exist; the spec is honest about them.

## What customers can claim, honestly

Combining the four components, a compliance customer can defensibly assert:

| Claim | Substantiated by |
|---|---|
| "Tool-call layer is fully governed by Powerbrain policy and audit" | MCP Connector (A) |
| "Every interaction with Claude Desktop is recorded for GDPR Art. 30 record-keeping" | Audit Mirror (B) |
| "Restricted-classified workstations have technical egress controls preventing direct Claude Desktop access" | Endpoint DLP (C) |
| "Users have a sanctioned mechanism to remove PII from prompts before submission" | Pre-flight (D) |
| "We can demonstrate evidence of prompt-side PII protection per incident" | A + B + D combined |

What they cannot claim:

| ❌ Inflated claim | Actual reality |
|---|---|
| "Every Claude Desktop prompt is pseudonymised at the wire" | Only prompts the user runs through Pre-flight are |
| "Claude never sees raw PII from our employees" | Depends on user discipline + Pre-flight adoption |
| "Powerbrain intercepts and rewrites all Claude Desktop traffic" | It does not — that path is closed by Anthropic policy |

This honest framing is what DPOs need. Inflated claims fail under scrutiny; honest claims with documented evidence chains pass.

## Decision record — what we deliberately do not build

Captured here so the question doesn't keep coming back:

| Considered approach | Decision | Reason |
|---|---|---|
| DOM-inject into claude.ai webview | **Rejected.** | Claude Desktop's Code tab is not a webview of claude.ai. Pseudonym Bridge spec (PR #174) targeted the wrong abstraction. |
| Modify Claude Desktop's `app.asar` | **Rejected.** | Consumer Terms Section 3 (reverse-engineering / modification). Also: Anthropic's auto-updater overwrites the patch. Compliance product cannot stand on a ToS violation. |
| Fork Claude Code / build OAuth proxy / third-party harness on Pro/Max | **Rejected.** | Feb 2026 Authentication policy + April 2026 enforcement explicitly target this class. |
| Repackage like aaddrick/claude-desktop-debian for our purposes | **Rejected.** | aaddrick relies on Anthropic's continued tolerance of a hobby Linux port. Powerbrain as commercial compliance product would not enjoy the same tolerance. |
| Use Claude Code CLI hooks on Pro/Max OAuth | **Deferred — not recommended for Code tab coverage.** | CLI hooks exist (`UserPromptSubmit`) but using them with Pro/Max OAuth in a commercial Powerbrain workflow conflicts with the spirit of the Feb 2026 policy. We do not actively recommend this to customers. |
| Run our own LLM gateway and ask users to point Claude Desktop at it | **Not possible.** | Claude Desktop's endpoint is hardcoded; `ANTHROPIC_BASE_URL` has no effect in the GUI. |

## Roll-out

| Phase | Deliverables | Customer-facing outcome |
|---|---|---|
| **1. Document + position** | This spec landed, [docs/editions.md](../editions.md) coverage-matrix updated, [docs/compliance-claude-desktop.md](../compliance-claude-desktop.md) refreshed with the 2026 policy moves, DLP recommendations in [docs/deployment.md](../deployment.md) | Sales / compliance teams have an honest pitch deck |
| **2. Audit Mirror MVP** | Implement [chat-audit-mirror plan](../plans/2026-05-20-claude-chat-audit-mirror.md) for macOS first, then Windows | Customers get the Art. 30 record-keeping claim immediately |
| **3. Pre-flight v1** | Tauri app, macOS + Windows, hotkey + popup UI, OS-keychain `pb_` key, signed installers | Customers can train users on a sanctioned pseudonymisation workflow |
| **4. Pre-flight v2** | Clipboard-offer mode, file-scan mode, project-resolve mode | Higher adoption via lower friction; addresses file-upload limitation |
| **5. Cross-device aggregation** | Audit Mirror reports per-user logs centrally to Powerbrain | Org-level compliance dashboards |

Each phase is independent — customers can buy / deploy in any order.

## Open questions

1. **Audit-mirror legality per jurisdiction.** Some employee-monitoring regulations require explicit consent before logging chat. Customer-side concern, but Powerbrain should provide a recommended consent flow (banner on Pre-flight first run, etc.).
2. **Storage format of Claude Desktop session logs.** Need to reverse-engineer (lawfully — accessing data the user already owns) the SQLite schema; assume it changes across versions. Adapter strategy similar to the DOM-adapter idea in the closed Pseudonym Bridge spec.
3. **MDM deployment story.** Compliance customers run MDM (Jamf, Intune). Pre-flight needs a silent-install / preconfigured-`pb_`-key story. Spec out as part of Phase 3.
4. **Pricing.** Pre-flight is an enterprise-tier feature. Is it bundled with `pb-proxy` license or separately licensed? Out of spec scope; product decision.
5. **Linux story.** Pre-flight on Linux is feasible (Tauri supports it). Claude Desktop is not available on Linux — those users use Claude Code CLI which already has API-key + `ANTHROPIC_BASE_URL=pb-proxy` as the supported path. So Pre-flight on Linux is value-additive for users of other AI tools, not for Claude Desktop coverage.

## Test plan

Doc + minor-component spec only. The single new buildable component (Pre-flight) gets its own implementation plan once approved.

- [ ] Spec reviewed against [docs/editions.md](../editions.md) three-paths matrix — does the coverage matrix here align?
- [ ] Spec reviewed against [docs/compliance-claude-desktop.md](../compliance-claude-desktop.md) — does the decision-record cite the right Anthropic policy timestamps?
- [ ] Compliance team confirms the "what customers can claim" table is defensibly accurate for German GDPR / EU AI Act framing
- [ ] Sales team confirms the strategy addresses the actual customer ask ("Pro/Max stays, Powerbrain dazwischen")
- [ ] Decision on Pre-flight tech stack (Tauri vs Electron vs native) before Phase 3 plan

## Out of scope (deliberately)

- Anything that intercepts, modifies, or proxies Claude Desktop's network traffic, process, binary, or UI
- Any product feature that relies on continued Anthropic tolerance of policy-violating patterns
- API-key-mode Claude Code coverage — already addressed by `pb-proxy` + `ANTHROPIC_BASE_URL`, not a Pro/Max scenario
- Replacing Claude Desktop with a Powerbrain-built coding agent
- Bridging non-Anthropic AI tools (ChatGPT, Gemini, Copilot). Pre-flight architecturally works for them too, but per-tool integration is a separate product question.
