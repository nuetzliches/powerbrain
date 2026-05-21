# Pseudonym Bridge — Spec

**Status:** Draft 2026-05-22, not yet approved
**Owner:** Powerbrain core team
**Related:** [docs/editions.md](../editions.md), [docs/compliance-claude-desktop.md](../compliance-claude-desktop.md), [docs/plans/2026-05-20-claude-chat-audit-mirror.md](../plans/2026-05-20-claude-chat-audit-mirror.md), [pb-proxy/pii_middleware.py](../../pb-proxy/pii_middleware.py)

## Motivation

The three-paths matrix in [editions.md](../editions.md#the-three-data-paths) closes ingest and tool-calls in both editions but leaves the **chat-content path** bypassed for clients that can't speak `pb-proxy`. The largest class of such clients is Anthropic's Claude Desktop on Pro/Max subscriptions: OAuth-bound to `claude.ai`, `ANTHROPIC_BASE_URL` is hardcoded, no API key is available to redirect through pb-proxy. Customers regulated under GDPR Art. 32 or the EU AI Act want the Pro/Max UX but cannot have raw PII reaching the LLM provider.

Two interception approaches were considered upstream:

1. **HTTP-layer interception** (Electron `session.webRequest` redirect to pb-proxy as auth-relay): forces pb-proxy to MITM Anthropic's edge, requires TLS-fingerprint spoofing, breaks under SSE response rewriting, and is fragile against frontend changes. Net value: read-only audit only.
2. **DOM-layer transformation** (this spec): pseudonymise text in the renderer **before** Claude.ai submits it, resolve pseudonyms in the rendered response **after** it arrives. Anthropic's wire only ever sees pseudonyms; PII never leaves the user's host.

This spec covers approach 2. It reuses the existing `/pseudonymize` and `/vault/resolve` endpoints without any backend changes.

## Goal

- Close the chat-content bypass for Claude Desktop / Claude.ai Pro/Max users on hosts running Powerbrain Enterprise.
- Stay backend-neutral — no new Powerbrain services, only new client code.
- Treat the bridge as **one distribution-form-agnostic feature** with two delivery channels: a desktop Electron wrapper and a browser extension.

## Non-Goals

- Replacing `pb-proxy`. Clients that can set `ANTHROPIC_BASE_URL` should continue to use the proxy directly.
- Working around Anthropic's Terms of Service. The bridge transforms the user's own input in the user's own client — it does **not** spoof requests, MITM TLS, or impersonate Claude Desktop.
- Hiding from Anthropic's edge. If Anthropic decides to detect or block this pattern, the bridge stops working; we will not add evasion logic.
- Subscription-less operation. The bridge requires a valid Claude Pro/Max session on the host; it does not bypass auth.
- Modifying Claude's responses semantically. We replace pseudonyms with originals — we do not censor, redact, or rewrite content the LLM produced.

## Scope

In scope:

- Electron wrapper application (`pb-bridge-desktop`) that loads `claude.ai` in a `BrowserWindow` with bridge preload.
- Browser extension (`pb-bridge-extension`) for Chromium and Firefox with `content_scripts` on `claude.ai`.
- Shared TypeScript core (`pb-bridge-core`) containing the DOM adapter, submit hook, render observer, streaming reassembly, attachment handler, and Powerbrain client.
- OPA policy package `pb.bridge` controlling activation, allowed roles, max payload size, attachment MIME allowlist.
- Settings UI for local `pb_` key storage, opt-out toggles, transparency-toast preferences.
- Prometheus metrics on bridge usage (proxied via existing Powerbrain services — bridge stays metrics-passive itself for privacy).
- Documentation: roll-out guide, threat model, capability-matrix update in [editions.md](../editions.md).

Out of scope (deliberately):

- Multi-account or workspace switching inside one bridge process — first version assumes one Claude session per bridge process.
- Native mobile apps (iOS/Android Claude) — DOM-layer hooks are not available there.
- Real-time PII verification with the semantic verifier ([docs/pii-verifier.md](../pii-verifier.md)) on every keystroke — too latency-sensitive; verifier runs only on the submit-path scan, not on type-ahead previews.
- Transparent UI replacement of Anthropic's chat composer — the bridge augments the existing composer, it does not render its own.
- Distribution via Anthropic-controlled channels. We ship our own installer and extension package.

## Architecture

### High-level flow

```
                ┌──────────────────────────────────────────────────────┐
                │                  Bridge client                        │
                │  (Electron BrowserWindow OR Browser content_script)   │
                │                                                       │
                │   ┌───────────────────────────────────────────────┐   │
                │   │              claude.ai DOM                    │   │
                │   │                                               │   │
   user types ─▶│   │  composer ◀── submit-hook ──▶ /pseudonymize ──┼───┼──▶ Powerbrain
                │   │                                               │   │   ingestion :8081
                │   │                                               │   │
                │   │  message-list ◀ render-hook ◀── /vault/resolve┼───┼──▶ Powerbrain
                │   │                                               │   │   mcp-server :8080
                │   └───────────────────────────────────────────────┘   │
                │                                                       │
                │   pb-bridge-core (TypeScript)                         │
                │   - DOM adapter (selector strategy)                   │
                │   - Streaming reassembly                              │
                │   - Powerbrain client (pb_ key, OPA-aware)            │
                └──────────────────────────────────────────────────────┘
                          │                                      │
                          │ pseudonymized prompt + attachments   │ pseudonymized response
                          ▼                                      ▲
                ┌──────────────────────────────────────────────────────┐
                │     claude.ai backend  →  api.claude.ai  →  LLM     │
                │     (Anthropic — sees only pseudonyms)               │
                └──────────────────────────────────────────────────────┘
```

### Components

| Component | Lives in | Responsibility |
|---|---|---|
| **DOM adapter** | `pb-bridge-core/dom/` | claude.ai selector strategy, composer/message-list discovery, mutation observers, version-detection for graceful degradation |
| **Submit hook** | `pb-bridge-core/submit/` | Intercept send (keyboard + button), call `/pseudonymize`, swap text in composer, display transparency toast |
| **Render hook** | `pb-bridge-core/render/` | Observe message-list mutations, buffer streaming tokens until pseudonym patterns are complete, call `/vault/resolve`, replace text in rendered nodes |
| **Streaming reassembly** | `pb-bridge-core/stream/` | Token buffer with `[TYPE:hash]` regex match across SSE chunks; release once a complete pseudonym or a non-pseudonym boundary is seen |
| **Attachment handler** | `pb-bridge-core/attach/` | Intercept file uploads (Electron via `session.webRequest`, extension via `webRequest`), pre-process via `/extract` + `/scan`, replace file payload with pseudonymized text injection |
| **Powerbrain client** | `pb-bridge-core/api/` | Thin wrapper around `/pseudonymize`, `/vault/resolve`, `/extract`, `/scan`; carries local `pb_` key; respects OPA decisions returned by mcp-server |
| **Settings UI** | distribution-specific | `pb_` key entry, per-conversation opt-out, transparency-toast level, attachment policy info |
| **Electron host** | `pb-bridge-desktop/` | Electron app, `webPreferences.preload` injection, OS-level secure storage for `pb_` key, optional auto-start |
| **Extension host** | `pb-bridge-extension/` | Manifest V3 extension, `content_scripts` injection, extension-storage for `pb_` key |

### Reuses existing Powerbrain primitives — no backend changes

| Bridge need | Powerbrain endpoint | Already used by |
|---|---|---|
| Pre-submit PII detection + pseudonymisation | `POST /pseudonymize` ([ingestion_api.py:1308](../../ingestion/ingestion_api.py)) | `pb-proxy/pii_middleware.py` |
| Post-render pseudonym → original resolution | `POST /vault/resolve` ([mcp-server/server.py:4303](../../mcp-server/server.py)) | `pb-proxy` agent loop |
| Attachment text extraction | `POST /extract` ([ingestion_api.py:1336](../../ingestion/ingestion_api.py)) | `pb-proxy` chat document attachments |
| Standalone PII scan (no pseudonymisation, for previews) | `POST /scan` ([ingestion_api.py:1271](../../ingestion/ingestion_api.py)) | demo Tab E preview |
| Activation + role gate | OPA `pb.bridge` (new) → mcp-server policy check | new |

The bridge is conceptually `pb-proxy/pii_middleware.py` lifted into the renderer. Same primitives, different layer.

## Wire protocol

### Pre-submit: `/pseudonymize`

```
POST {INGESTION_BASE}/pseudonymize
Authorization: Bearer pb_<bridge-key>
Content-Type: application/json

{
  "text": "<user-typed prompt>",
  "salt": "<per-conversation salt, stored in extension/electron settings>"
}
```

Response (unchanged from today):

```json
{
  "text": "<pseudonymised prompt>",
  "contains_pii": true,
  "mapping": { "Sebastian": "a1b2c3d4", "sebastian@example.com": "f9e8d7c6" },
  "entities": [ { "type": "PERSON", "score": 0.95, "start": 0, "end": 9 } ]
}
```

The bridge stores `mapping` in renderer memory keyed by `conversationId`. It is **not** persisted: a reload re-pseudonymises history via `/vault/resolve` based on what's stored on Anthropic's side.

Session-salt strategy: one salt per Claude conversation, derived from `conversationId + bridge-install-secret`. This keeps pseudonyms stable for the lifetime of one conversation (so the LLM can refer back to `[PERSON:a1b2c3d4]` across turns) but rotates between conversations.

### Post-render: `/vault/resolve`

```
POST {MCP_BASE}/vault/resolve
Authorization: Bearer pb_<bridge-key>
X-Purpose: claude_chat
Content-Type: application/json

{
  "text": "<assistant message containing [TYPE:hash] tokens>",
  "purpose": "claude_chat"
}
```

Response: text with resolved pseudonyms, plus per-resolution audit info already produced today.

OPA gates the resolution per `(role, purpose, classification, data_category)`. If the bridge's `pb_` key lacks the role for a given purpose, resolution returns pseudonyms unchanged — bridge falls back to displaying the pseudonym, optionally with a hover-tooltip ("Vault-resolution denied: purpose claude_chat not permitted for role viewer").

### Attachments: `/extract` + `/scan`

When the user attaches a file:

1. Bridge intercepts the upload before it reaches `claude.ai`.
2. Sends file (base64) to `POST /extract`.
3. Sends extracted text to `POST /scan`.
4. If `contains_pii=true`: pseudonymises locally (reusing the conversation salt + `/pseudonymize`), then injects the pseudonymised text as a **markdown code block in the composer**, not as a file upload to Claude.
5. The file itself is never sent to Anthropic.

Bypass path: if OPA `pb.bridge.attachments.passthrough_allowed` returns true (e.g. for `public` classification), the original file flows through unchanged. Default: pseudonymise.

### Auth: local `pb_` key

The bridge holds **one local `pb_` key**, configured at install time. This key authenticates bridge → Powerbrain calls only. It is never sent to `claude.ai`.

Storage:

- **Electron:** `safeStorage.encryptString()` (OS keychain — Keychain on macOS, libsecret on Linux, DPAPI on Windows).
- **Extension:** `chrome.storage.session` (in-memory across browser session) for normal use; optional `chrome.storage.local` for persist-on-restart with user opt-in.

Refusal mode: if the key is missing or rejected by Powerbrain, the bridge enters **read-only audit mode** (observes prompts and responses, logs to a local audit file under `~/.pb-bridge/`, does **not** modify the DOM). User sees a persistent banner: "Powerbrain not configured — chat content is going to Anthropic unfiltered."

## DOM integration

### Selector strategy

The bridge does **not** rely on hard-coded CSS selectors. Instead, it ships a **selector adapter** with a fallback ladder:

1. ARIA-role lookup: `role="textbox"` for the composer, `role="log"` for the message list.
2. Stable `data-*` attributes Anthropic has shipped (audited at adapter-build time).
3. Heuristic fallback: largest contentEditable with a sibling submit button.

The adapter ships with a snapshot of the current claude.ai DOM (HTML fixture in `pb-bridge-core/dom/fixtures/`) and a CI job that re-runs the adapter against fresh fixtures pulled nightly. Drift between adapter and real DOM triggers a banner: "Powerbrain bridge needs an update."

### Submit interception

```
composer.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    e.stopImmediatePropagation();
    await pseudonymiseAndSubmit(composer);
  }
}, { capture: true });
```

`capture: true` ensures we run before Anthropic's own handler. The submit button gets equivalent treatment via `pointerdown` capture.

`pseudonymiseAndSubmit`:

1. Read `composer.textContent`.
2. Call `/pseudonymize`.
3. Replace the composer text via `document.execCommand('insertText', ...)` or the React-aware text-injection (the composer is React-controlled; naive `innerHTML` swap loses focus and breaks React state — see *Open questions*).
4. Show transparency toast: "Powerbrain replaced N PII tokens before sending."
5. Dispatch the same `KeyboardEvent` that we cancelled, with `bridge:processed=true` marker so we don't re-loop.

### Render observer

```
const messageList = adapter.findMessageList();
new MutationObserver(handleMessageMutations).observe(messageList, {
  childList: true, subtree: true, characterData: true
});
```

`handleMessageMutations` feeds new text into the streaming reassembly buffer. When the buffer emits a "complete" chunk (= no open pseudonym pattern), we call `/vault/resolve`, then patch the text nodes in place. The DOM patch preserves React's virtual-DOM by using `Range.surroundContents` rather than replacing the node — keeps Anthropic's event handlers attached.

## Streaming reassembly

SSE delivers Claude's response token-by-token. A pseudonym like `[PERSON:a1b2c3d4]` is 18 characters and likely spans 2–4 tokens. Naive per-mutation resolve would either:

- See `[PERSON:a1b2` and try to resolve it (fails silently), or
- Resolve the same complete pseudonym multiple times.

Buffer protocol:

```
state = "scanning"
buffer = ""

on each mutation chunk c:
  buffer += c
  while buffer contains either:
    - a complete pseudonym pattern [A-Z_]+:[a-f0-9]{8}]
    - or a confirmed non-pseudonym boundary (whitespace/punctuation not inside a [...])
  → emit complete prefix to /vault/resolve, keep tail in buffer
```

A 200ms idle timer flushes whatever's left when the stream ends. The reassembly tests use a fixture stream from a recorded claude.ai SSE session.

## OPA policy extension

New package `pb.bridge` (file: `opa-policies/pb/bridge.rego`):

```rego
package pb.bridge

# pb.bridge.enabled — master switch (default false; admin enables per deployment)
enabled := data.pb.config.bridge.enabled

# pb.bridge.activation_allowed(input) — gates whether a bridge install can authenticate
activation_allowed if {
  enabled
  input.agent_role in data.pb.config.bridge.allowed_roles
  input.host in data.pb.config.bridge.allowed_hosts  # "claude.ai", "claude.com"
}

# pb.bridge.attachment_allowed(input) — per-MIME, per-classification
attachment_allowed if {
  input.mime in data.pb.config.bridge.attachments.mime_allowlist
  input.bytes <= data.pb.config.bridge.attachments.max_bytes
}
```

`opa-policies/data.json` additions (under `pb.config.bridge`):

```json
"bridge": {
  "enabled": false,
  "allowed_roles": ["analyst", "developer", "admin"],
  "allowed_hosts": ["claude.ai", "claude.com"],
  "default_purpose": "claude_chat",
  "attachments": {
    "passthrough_allowed_classifications": ["public"],
    "mime_allowlist": ["application/pdf", "text/plain", "image/png",
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    "max_bytes": 26214400
  },
  "transparency": {
    "toast_level": "on_pii_detected",
    "show_pseudonym_count": true
  }
}
```

Editable at runtime via `manage_policies` MCP tool (admin only). Same JSON-schema-validated pattern as other `pb.config.*` sections.

## UX

### Transparency toast

On every submit, the bridge briefly shows a non-modal toast in the corner of the claude.ai window:

> Powerbrain protected 3 PII tokens before sending.

Three levels controlled by `pb.config.bridge.transparency.toast_level`:

- `always` — toast on every submit, including zero-PII.
- `on_pii_detected` (default) — toast only when ≥1 token replaced.
- `silent` — no toast; bridge logs to local audit only. For compliance customers who don't want users to see Powerbrain branding inside Claude.

### Hover preview before send

Optional (off by default): when the user has typed but not yet sent, a small "Powerbrain preview" icon next to the send button reveals a tooltip showing what Anthropic will actually receive. Useful for high-trust environments where users want to verify the transformation. Off by default to avoid latency on every keystroke.

### Conversation-level opt-out

A toggle in the bridge's settings UI: "Skip Powerbrain for this conversation." Stored per `conversationId`. Useful for low-sensitivity threads. Audit log still records the opt-out decision (who, when, which conversation).

### Settings UI

Single page accessed via system tray (Electron) or extension popup (browser). Fields:

- Powerbrain MCP URL + `pb_` API key
- Default purpose (one of `pb.config.bridge.allowed_purposes` — defaults to `claude_chat`)
- Transparency-toast level
- Per-conversation opt-out list (read-only, populated by user actions)
- Bridge version, adapter version, last DOM-fixture validation

## Telemetry & audit

The bridge itself is metrics-passive — it does not run a Prometheus exporter. All telemetry flows through Powerbrain endpoints, which already emit metrics on every call:

| Metric (new) | Where emitted | Labels |
|---|---|---|
| `pb_ingestion_pseudonymize_total` | ingestion | `caller=bridge` (new label value, no schema change) |
| `pb_mcp_vault_resolve_total` | mcp-server | `caller=bridge`, `purpose` |
| `pb_bridge_dom_adapter_version` | mcp-server | `version` — pushed on bridge start-up via `POST /telemetry/bridge` (one new tiny endpoint) |

Audit log: every `/pseudonymize` and `/vault/resolve` call already emits an `agent_access_log` row. Bridge calls are indistinguishable from `pb-proxy` calls in the audit trail — both authenticate with `pb_` keys, both record `agent_id`. We add a `client_type` field to `agent_access_log` (`proxy` | `bridge` | `direct_mcp`) so that the DPO can filter the audit chain per channel.

This is the **only** backend change in the spec: one nullable column in `agent_access_log`. Forward-compatible, defaults to NULL for existing rows.

## Threat model

| Asset | Threat | Mitigation |
|---|---|---|
| User-typed PII | Sent to Anthropic in plaintext | Pre-submit pseudonymisation via `/pseudonymize` |
| Vault originals | Read by an unauthorised renderer | Local `pb_` key + OPA per resolution; vault stays in PostgreSQL RLS, never traverses the renderer |
| `pb_` key | Stolen from local storage | OS keychain (Electron) or session-only storage (extension); rotatable; audit-log shows last use |
| Bridge install integrity | Attacker patches preload script to disable pseudonymisation | Out-of-scope for v1; addressed via OS-level package signing + checksum in Powerbrain admin UI |
| Anthropic edge detection | Anthropic identifies bridge clients and blocks them | Accepted risk — bridge does not impersonate Claude Desktop or evade detection; if blocked, bridge degrades to read-only audit mode |
| DOM-injection bypass | Anthropic changes selectors mid-session | Selector adapter with fallback ladder; user-visible banner on drift; CI fixture refresh |
| Pseudonym leakage to other tabs | Conversation salt + reverse-map leaks via shared origin | Bridge isolates per `conversationId`; salt rotates between conversations |

What the bridge does **not** protect against:

- Anthropic's edge logging the pseudonymised text (we *want* them to see only pseudonyms — but they still see the prompt structure and any non-PII content).
- Side-channel inference: the LLM may guess at originals from context even when pseudonymised. The pseudonym strategy uses typed tokens specifically to reduce this risk, but cannot eliminate it.
- Endpoint compromise: malware on the user's machine can read both the composer text and the bridge state.

## Roll-out stages

| Stage | What ships | Goal |
|---|---|---|
| **1. Read-only audit** | Electron + extension observe submits and responses, log to local audit file, no DOM modification, banner says "audit mode" | Prove DOM adapter stability on real claude.ai traffic without affecting users |
| **2. Submit-side pseudonymisation** | Add `/pseudonymize` call before submit, swap text in composer, transparency toast | Close inbound PII channel; outbound responses still contain pseudonyms |
| **3. Bi-directional + tool-call awareness** | Add render observer + streaming reassembly + `/vault/resolve`; handle conversation history reload | Full bi-directional protection; matches `pb-proxy` chat behaviour |
| **4. Attachments** | Intercept file uploads, route through `/extract` + `/scan`, replace with pseudonymised inline text | Close file-upload PII channel |
| **5. Distribution polish** | Signed installers for Electron, extension-store submissions, auto-update channel | GA for compliance customers |

Each stage ships behind a `pb.config.bridge.stage` policy switch so an admin can pin a deployment to an earlier stage during rollout.

## Edition & boundary update

The bridge is **Enterprise-only** because it depends on `/vault/resolve`, which is part of the Enterprise capability surface ([editions.md](../editions.md#capability-matrix) row "Vault resolution for tool-call pseudonyms").

When this spec lands as an implementation, [docs/editions.md](../editions.md) the-three-data-paths matrix gains a fourth column "Enterprise + Pseudonym Bridge" and the chat-content row becomes:

| Path | Community | Enterprise (proxy) | Enterprise (bridge) |
|---|---|---|---|
| Chat content | ❌ Bypass | ✅ Wire-level pseudonymisation in pb-proxy | ✅ DOM-level pseudonymisation in claude.ai client |

This update happens in the implementation PR, not in this spec PR.

## Open questions

1. **React state vs DOM patch.** Anthropic's composer is a controlled React component. `document.execCommand('insertText')` works but is deprecated; React-aware text-injection (dispatching `InputEvent` with `inputType=insertText`) is the modern path. Need to test on current claude.ai.
2. **Conversation history on reload.** When the user reopens an old conversation, claude.ai re-renders the message history server-side. The render observer must run a one-shot resolve pass on initial-render, then switch to incremental mode. How to detect initial-render reliably?
3. **Tool calls and computer use.** Claude can produce structured tool-call JSON in its response. Naive text-replace of pseudonyms inside JSON could break parsing. Resolution: detect JSON-fenced regions and skip them, or parse-and-replace per JSON field. Defer to Stage 3.
4. **Multi-conversation salt rotation.** If the user starts a new conversation from a forked turn, what's the right salt? Probably: forks inherit the parent's salt; new top-level conversations get a fresh salt.
5. **Browser-extension Manifest V3 limits.** MV3 service workers have lifetime caps. Long-lived conversation state needs to move to `chrome.storage.session`. Verify before committing to extension delivery.
6. **Telemetry endpoint `POST /telemetry/bridge`.** This is the only new backend surface. Could we use `agent_access_log` directly via a synthetic call? Or accept one new tiny endpoint? Lean toward the latter for clarity.

## Test plan

| Layer | Test |
|---|---|
| Selector adapter | Snapshot tests against `dom/fixtures/claude-ai-2026-05-22.html` (and rolling fixtures) |
| Streaming reassembly | Replay recorded SSE streams; assert no pseudonym is resolved twice or split |
| Submit hook | Headless Electron + Puppeteer: type → assert composer text swapped → assert send |
| Render observer | Headless: inject pseudonymised message → assert resolved text replaces it |
| Attachment handler | Upload a PDF with known PII → assert `/extract` + `/scan` calls → assert composer receives pseudonymised inline text |
| OPA policy `pb.bridge` | OPA test file with 6+ activation cases (allowed/denied roles, allowed/denied hosts, disabled-master) |
| Powerbrain client | Unit tests for retry, OPA-denial handling, audit-mode fallback |
| End-to-end | Manual smoke against a local claude.ai instance? Not possible — Anthropic-controlled domain. Manual checklist instead, documented in `docs/bridge-manual-test.md` |

CI coverage target: 80% for `pb-bridge-core` (matches Powerbrain backend threshold).

## Glossary

- **Bridge**: this client-side component, in any of its distribution forms.
- **Adapter**: the part of the bridge that knows the current claude.ai DOM structure.
- **Conversation salt**: per-conversation random value used to derive pseudonym hashes, so the same person consistently maps to the same `[PERSON:abc]` token within one conversation.
- **Read-only audit mode**: bridge logs activity but does not modify any DOM. Used when Powerbrain is misconfigured or stage 1 is pinned.

## Out of scope (deliberately)

- Bridging non-Anthropic chat UIs (ChatGPT, Gemini, Copilot). Conceptually identical, but per-vendor adapter work. Will be tracked as separate specs if/when demand exists.
- Bridge-to-bridge synchronisation across multiple devices. Each install is independent.
- Replacing Powerbrain's `pb_` key auth with OAuth. Future work, tracked under identity-hardening.
- Native Claude Desktop replacement. We embed claude.ai; we do not rebuild it.
