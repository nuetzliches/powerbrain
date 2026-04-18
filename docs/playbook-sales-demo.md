# Sales Demo Playbook

A 15-minute live walkthrough of Powerbrain for decision-makers: legal,
procurement, CTO/CISO audiences. The goal of this demo is not to show
code — it is to answer three questions that typically block enterprise
AI pilots: *"Who can see what?"*, *"Where does the PII go?"*, and *"Can
the agent tell us who to talk to next?"*

## 1. Run the demo stack (once)

```bash
./scripts/quickstart.sh --demo
```

This enables the `seed` and `demo` Docker Compose profiles. The script
- generates random Docker Secrets for PostgreSQL + the vault HMAC key,
- pulls the embedding model,
- creates Qdrant collections,
- seeds 21 base documents + 6 German customer records (with PII) + an 8-person org-chart graph,
- prints endpoints including **Demo UI → http://localhost:8095**.

Wait until the banner appears before opening the browser.

## 2. Layout

| URL | Purpose |
|---|---|
| `http://localhost:8095` | The demo UI (five tabs) |
| `http://localhost:8090/health` | pb-proxy — shows `"edition": "enterprise"` |
| `http://localhost:3001` | Grafana (`admin` / `admin`) — switch to the *Overview* dashboard for a live side-show |
| `http://localhost:6333/dashboard` | Qdrant UI — optional, show a collection payload to prove the vector store carries pseudonyms |

## 3. The narrative (15 minutes)

### Opening (1 min)

> "Most enterprise AI pilots stall at legal review. The questions are always the same: who can see which data, where does personal data end up, and can we prove it. Powerbrain is the context layer that makes that approval meeting short. Everything you are about to see runs locally — no cloud dependency."

### Tab A — Same question, different answers (4 min)

**Story:** Role-based access is not bolted on; it runs inside every search.

1. Type `Gehaltsbänder` and click **Run query**.
2. Left column (analyst) shows the confidential salary-band document. Right column (viewer) is empty.
3. Expand *What just happened?* and read the four-step breakdown aloud.
4. Try `Kundenliste` — same effect: analyst sees confidential customer records, viewer sees none.
5. Try `Onboarding-Checkliste` — both roles see it because it is `public`.

**Talking point:** "We did not write any filter code. The role sits inside the API key, OPA evaluates `pb.access.allow`, and only allowed hits reach the reranker. Swapping the key is the only change between left and right."

### Tab B — We never stored the secret (6 min)

**Story:** A document goes in, personal data never reaches the vector store, but we can still reveal the original with purpose-bound auditable access.

1. **Step 1 · Scan.** Click **Scan (detect PII)**. Presidio reports which entities it found (`PERSON`, `EMAIL_ADDRESS`, `IBAN_CODE`, `LOCATION`, `PHONE_NUMBER`, `DATE_OF_BIRTH`) and shows the masked version. Emphasise: *"Nothing is stored yet."*
2. **Step 2 · Ingest + Search.** Click **Ingest record**, then **Search with analyst key**. Walk the audience through the result: the content is the masked text, metadata carries a `vault_ref` and `classification=confidential`.
3. **Step 3 · Reveal.** Pick `purpose=support`, click **Reveal (with vault token)**. The previously masked entry is unlocked; the *original content* panel shows the real name and IBAN. Point out the issued token preview: purpose binding, short TTL, signature.
4. Expand *Show recent audit entries for this agent* — every access left a trace.

**Talking points:**
- "The embedding is computed on the masked text, so the vector model never sees the PII."
- "The token is HMAC-signed with a secret that lives only on the trusted service. No key, no unlock."
- "Right to be forgotten (Art. 17) is a `DELETE FROM pii_vault.original_content WHERE document_id=…`. The pseudonym in Qdrant remains, but the original is gone — and mathematically irretrievable."

### Tab C — The org behind the answer (2 min)

**Story:** Vector search finds *what*. Graph context tells you *who*.

1. Select an employee (e.g. *Elena Hartmann — VPEngineering*).
2. Move the traversal depth to 2. The graph shows the employee, her Department, and every Project with allocation edges.
3. Pick a sales person (e.g. *Tim Heller*). Show that the graph now includes the Sales department and the CRM-integration project.

**Talking point:** "An agent can use this to answer 'who owns the platform migration?' or 'which team do I loop in for this ticket?' — the answer is one graph hop away, no prompt engineering."

### Tab D — MCP vs Proxy (3 min, optional)

**Story:** "And here's how this changes the moment you put our proxy in front of MCP."

1. Switch to Tab D. Pick the "Fasse die Daten zu unserem Kunden Julia Weber zusammen" suggestion.
2. Left column: raw MCP response — pseudonyms everywhere (`[PERSON:xxx]`, `[EMAIL_ADDRESS:xxx]`).
3. Right column: pb-proxy call. LLM produced a natural-language summary with Julia's real name resolved via `/vault/resolve` under `purpose=support`; IBAN and address stayed pseudonymised because the policy says `support` doesn't need them.
4. Toggle purpose to `billing` → IBAN resolves too, DOB is still masked.

**Talking point:** "The MCP path is the compliant data layer — our **community** edition. Adding pb-proxy turns it into chat-native UX without your team writing orchestration code — our **enterprise** edition. Both run on your infrastructure. The only switch is `docker compose --profile proxy`."

### Tab E — Pipeline Inspector (3 min, optional)

**Story:** "And this is what happens to a document in the first place — regardless of where it came from."

1. Switch to Tab E, keep the default fixture *SharePoint — Rahmenvertrag 2026*. Hit **Run dry-run**.
2. Walk through the four phases that appeared:
   - **Extract**: the DOCX/MD/EML binary would go through markitdown → plain text. For this fixture we skip the binary step because we already have text.
   - **PII Scan**: entity badges appear (PERSON, EMAIL_ADDRESS, IBAN_CODE, LOCATION, DE_DATE_OF_BIRTH, DE_TAX_ID). Click *First detected entities* to show Presidio's confidence scores per hit.
   - **Quality Gate**: the composite score + factor breakdown. "EU AI Act Art. 10 — every document clears this gate or it gets rejected before embedding."
   - **OPA Privacy**: the decision. Confidential + PII + legal basis → `encrypt_and_store` (vault). Remove the legal basis in the form → decision flips to `block`.
3. Switch to the *Outlook — Customer support request* fixture → see how an `internal` classification with PII produces `pseudonymize` + dual storage.
4. Switch to *GitHub — adapter-template README* → see `mask`, no PII, no vault. Shows every adapter funnels into the same pipeline.
5. Upload option: let the customer drop a representative file of their own (NDA, invoice, meeting notes) and hit **Run dry-run**. Nothing is persisted.

**Talking point:** "Nothing you see here has been written to the knowledge base. Pick your adapter — GitHub, SharePoint, Outlook, OneNote, Teams — every document walks these same four phases. If you can show us a representative document from your environment, we can tell you right now which classification and which legal basis your own policies will produce."

### Close (2 min)

- **Open-source, self-hosted, GDPR-native.** Everything fits on a laptop or an on-prem cluster.
- **Policies are data, not code.** `opa-policies/data.json` is editable JSON, validated by JSON Schema.
- **MCP-native.** The same protocol that drove this demo is what your agents will use in production — no custom integrations per tool.
- **Two tiers, one codebase.** See [docs/editions.md](editions.md) for the full community/enterprise matrix.

Invite the audience to fork the repo and run `./scripts/quickstart.sh --demo` themselves.

## 4. Troubleshooting

| Symptom | Fix |
|---|---|
| Tab A returns empty for both roles | Seed did not run. `docker compose --profile seed logs seed`. Re-run `./scripts/quickstart.sh --demo`. |
| Tab B reveal says "pseudonymous" for all hits | Ingested document predates the vault feature, or its `data_category` is missing. Click **Ingest record** again to create a fresh vault-backed entry, then retry the reveal. |
| Tab B reveal errors "VAULT_HMAC_SECRET not available" | The demo container cannot read the HMAC secret. `ls secrets/vault_hmac_secret.txt` should show a non-empty file. Restart: `docker compose --profile demo up -d pb-demo`. |
| Tab C shows "No employees found" | Graph seed did not run. `docker compose --profile seed logs seed` and check for errors in the *Graph seed* section. |
| Tab D shows "pb-proxy not reachable" | The proxy isn't up. `docker compose --profile demo up -d pb-proxy` or restart the full stack via `./scripts/quickstart.sh --demo`. |
| Tab D proxy response looks hallucinated / LLM ignored tool | `qwen2.5:3b` sometimes skips tool calls on the first turn. Ask a more direct question ("search_knowledge für Kunde Julia") or switch `PROXY_MODEL` to `claude-opus` / `gpt-4o` in the demo service env. |
| Tab E "No fixtures available" | Rebuild the demo image so `demo/fixtures/` gets packaged: `docker compose --profile demo build pb-demo && docker compose up -d --force-recreate pb-demo`. |
| Tab E dry-run reports `would_ingest=False` unexpectedly | The form's `classification` / `source_type` / `legal_basis` fields are editable — check the values above the result. Presidio also flags harmless-looking strings sometimes; use the entity breakdown to confirm. |
| "Demo out of date — MCP response shape changed" | The demo UI's Pydantic models are behind a newer MCP server. Rebuild: `docker compose --profile demo build pb-demo`. |

## 5. What to skip if you have only 5 minutes

1. Skip Tab A's viewer query and jump straight to Tab B.
2. In Tab B, skip the scan step and click **Ingest record** + **Reveal (with vault token)** in one motion.
3. End after Tab C's single employee traversal.

## 6. After the call

Leave the customer with:
- This playbook: [docs/playbook-sales-demo.md](playbook-sales-demo.md)
- Technical deep-dive: [docs/architecture.md](architecture.md)
- Compliance posture: ask for the live `generate_compliance_doc` output via the MCP tool
- An invitation to run `./scripts/quickstart.sh --demo` on their own hardware
