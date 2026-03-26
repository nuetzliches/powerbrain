# GDPR: Personal Data and External AI Services

## Question

Does a data protection violation under GDPR occur when personal data
is transmitted to claude.ai or comparable external LLM services?

---

## Short Answer

**Yes** — always with claude.ai (Consumer). With the Anthropic API under
certain conditions it is legally arguable, but with residual risk.

---

## The Three Requirements

### 1. Data Processing Agreement (Art. 28 GDPR)

Anyone who passes personal data to a third party for processing
**must** conclude a DPA. Without a DPA, the transfer is a violation —
regardless of the recipient.

| Service | DPA available? |
|--------|----------------|
| Anthropic API (Enterprise/Team) | Yes, can be concluded via the console |
| claude.ai (Consumer) | **No** — Anthropic acts as its own controller there, not as a data processor |

For **claude.ai** there is structurally no DPA path. This alone
rules out compliant use for data involving personal references.

### 2. Third-Country Transfer USA (Art. 44–49 GDPR)

Anthropic is a US company. Since *Schrems II* (ECJ, July 2020), every
transfer to the USA requires its own legal basis:

**EU-US Data Privacy Framework (DPF)**
Adequacy decision by the EU Commission since July 2023. US companies
that are certified may receive data without SCCs.
→ Check: [dataprivacyframework.gov](https://www.dataprivacyframework.gov)
→ Status August 2025: Anthropic certification **not confirmed** — verify
before use.

**Standard Contractual Clauses (SCCs)**
Anthropic offers SCCs in the API enterprise context. Since *Schrems II*,
SCCs alone are formally insufficient — a **Transfer Impact Assessment (TIA)** is
required, which evaluates the actual possibility of US government access
(CLOUD Act, FISA 702). A *Schrems III* cannot be ruled out.

**Art. 49 exceptions** (explicit consent, contract fulfillment, etc.) are
not viable for systematic AI use.

### 3. Data Use for Training

claude.ai (Consumer) reserves the right in its terms of service to use
conversations for model training. Even if the transfer were legal:
Passing data on for training purposes would be a purpose limitation violation
(Art. 5(1)(b)) and requires a new legal basis that typically does not exist
for the original data.

---

## Assessment Matrix

| Scenario | Assessment |
|----------|-----------|
| claude.ai (Browser/Consumer) with personal data | **Clear violation** — no DPA possible, training data risk |
| Anthropic API without DPA + SCCs | **Violation** — missing contractual basis |
| Anthropic API with DPA + SCCs, without TIA | **Likely violation** |
| Anthropic API with DPA + SCCs + TIA + no training | Legally arguable, residual risk from CLOUD Act |
| This project (Ollama local) | ✅ No transfer, no problem |

---

## Relevance for the Development Workflow

This project deliberately uses **Ollama locally** — the architectural decision
"embeddings local" structurally rules out external data transmission.

Consequently, this also applies to the development workflow:
**No real data in prompts to external AI assistants** (code assistants,
chats), not even as test data in schemas or as examples in code comments.

When Claude Code or comparable tools are used for development:
- Use only anonymized/synthetic sample data in the project context
- No production dumps, real customer numbers or names as test data

---

## Legal Classification: "Sweeping It Under the Rug" vs. Documenting

→ Separate treatment in [architecture.md §4.5](architecture.md) and
[006_privacy_incidents.sql](../init-db/006_privacy_incidents.sql).

Short version: Not logging does not create protection:
- The 72h deadline (Art. 33) runs from the moment of becoming aware, not from the decision
- Authorities (BfDI — Federal Commissioner for Data Protection and Freedom of Information; LfDI — State Commissioner for Data Protection and Freedom of Information) impose 2–4× higher
  fines upon subsequent discovery than upon proactive notification
- §42 BDSG: personal criminal liability (up to 3 years) for willful failure to report
- Art. 5(2) accountability obligation requires active documentation

---

## Sources and References

- GDPR Art. 5, 6, 28, 33, 34, 44–49, 83
- BDSG §42, §43
- ECJ C-311/18 (*Schrems II*, 16 July 2020)
- EU Commission Implementing Decision (EU) 2023/1795 (EU-US DPF, 10 July 2023)
- EDPB Recommendations 01/2020 on supplementary measures for transfers
- BfDI (Federal Commissioner for Data Protection and Freedom of Information): Guidance on AI Language Models (check for current version)
