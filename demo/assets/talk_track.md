### 15-minute talk track

**Open (1 min).** "Most enterprise AI pilots stall at legal review. Powerbrain is the context layer that makes the approval meeting short."

**Tab A · Same question, different answers (4 min).**
Pick `Gehaltsbänder`. Run with analyst → you see a confidential salary-band hit. Run with viewer → zero hits. No code changed between runs; only the API key (= the role). "Your agents inherit this permission model without writing any access code."

**Tab B · We never stored the secret (6 min).**
Step 1 *Scan* — Presidio flags names, email, IBAN, address. Nothing is stored yet.
Step 2 *Ingest + Search* — the Qdrant payload carries pseudonyms like `<PERSON>`, `<IBAN_CODE>`. The search works because the embeddings were built on the *context*, not on the PII.
Step 3 *Reveal* — a short-lived HMAC token with `purpose=support` unlocks the original via OPA. Every access lands in the audit log. "This is the Art. 17 story: delete the vault row and the pseudonym becomes irreversible — the right to be forgotten without re-training anything."

**Tab C · The org behind the answer (2 min).**
Pick an employee → the graph shows the department and the projects they touch. "Vector search finds *what*. The graph tells you *who*. Agents that combine both give concrete next-step answers instead of paragraphs of prose."

**Close (2 min).**
Remind the room:
- All components are open source, self-hosted, GDPR-native.
- OPA policies are editable JSON — no Rego knowledge required.
- The same MCP server that fed this demo is what agents will use in production.
