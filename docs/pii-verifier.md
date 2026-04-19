# Semantic PII Verifier

> **Status:** Optional feature, community default `off`. Enable via
> `pb.config.ingestion.pii_verifier.enabled = true` (admin, runtime-
> editable through the `manage_policies` MCP tool).

## Why this exists

Presidio is the right Recall layer. On German business text it
flags every capitalised noun as a PERSON or LOCATION candidate,
which is exactly what a GDPR regulator would ask for. It is poor at
Precision: words like `Zahlungsstatus`, `Geschäftsführer`,
`Rahmenvertrag`, and every Sparkasse branch name end up tagged.

Live observation on the NovaTech fixture set:

* Recall: 100% of genuine PII caught.
* Precision on `confidential` customer chunks: roughly 60% — every
  second hit was a noun or role.

Two kinds of failure cost us real money:

| Failure | Cost |
|---|---|
| False **Negative** — real PII leaked | GDPR incident, €20M fine tier |
| False **Positive** — business text over-masked | Blocked documents, broken UX, demo-dead |

Presidio alone is biased toward false positives on German. Tuning
(deny-lists, lower-per-entity thresholds) helps bounded amounts. A
*semantic* layer that understands context — "`Geschäftsführer` is a
role, not a person" — fixes the precision tail without touching
recall.

## Architecture

```
  Text
    ↓
  ┌───────────────────────────────────────┐
  │  Presidio scan_text                   │
  │  • patterns (IBAN / email / phone)    │   ←  Recall layer
  │  • spaCy NER (PERSON / LOCATION)      │
  │  • custom recognizers (DE DOB, …)     │
  └──────────┬────────────────────────────┘
             │ entity_locations[]
             ↓
  ┌───────────────────────────────────────┐
  │  _resolve_overlapping_spans()         │   ←  Dedup (existing)
  └──────────┬────────────────────────────┘
             │
             ↓
  ┌───────────────────────────────────────┐
  │  Semantic Verifier (this doc)         │   ←  Precision layer
  │                                       │
  │  PATTERN_TYPES pass through           │
  │   (IBAN / email / phone / DOB / …)    │
  │                                       │
  │  Ambiguous types → LLM batch:         │
  │   "Welche dieser Kandidaten sind      │
  │    echte PII im Kontext?"             │
  │                                       │
  │  Verdicts merged, rejected removed.   │
  └──────────┬────────────────────────────┘
             │ filtered entity_locations[]
             ↓
      quality gate → OPA privacy → pseudonymise → vault
```

### Design rules

1. **Pattern types are never second-guessed.** IBAN_CODE,
   EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD, IP_ADDRESS,
   DE_TAX_ID, DE_SOCIAL_SECURITY, DE_DATE_OF_BIRTH. Presidio's
   score on these is already high-precision.
2. **Batch per document.** One LLM call regardless of candidate
   count. On qwen2.5:3b locally that's ~250–400 ms per
   call — unchanged whether the document has 3 or 30 candidates.
3. **Fail-open.** Any LLM timeout / malformed JSON / 500 / parse
   error → keep every original candidate. Recall is never
   sacrificed for precision.
4. **Low temperature.** Temperature 0, deterministic decoding.
   Same document with same prompt produces the same verdict.
5. **Contained context.** The LLM only sees ±60 chars around each
   candidate, not the full document. PII visibility stays local.

## Configuration

`opa-policies/data.json`:

```json
"ingestion": {
  "pii_verifier": {
    "enabled":             true,
    "backend":             "llm",
    "min_confidence_keep": 0.5
  }
}
```

Runtime env vars (map 1:1 to constructor args):

| Env var | Default | Description |
|---|---|---|
| `PII_VERIFIER_BACKEND` | `noop` | `noop` or `llm`. OPA config wins at runtime. |
| `PII_VERIFIER_URL` | `${LLM_PROVIDER_URL}` | Chat endpoint base URL. |
| `PII_VERIFIER_MODEL` | `${LLM_MODEL}` | Model name (e.g. `qwen2.5:3b`). |
| `PII_VERIFIER_API_KEY` | `${LLM_API_KEY}` | Bearer token if the backend needs one. |
| `PII_VERIFIER_TIMEOUT_SECONDS` | `15` | Per-request timeout. |

`backend=noop` is the community default: the verifier layer is
present in the codebase but forwards every candidate unchanged, so
the pipeline behaves exactly as it did pre-verifier. `backend=llm`
with a reachable chat endpoint activates the precision filter.

## Observability

### Prometheus

```
pb_ingestion_pii_verifier_calls_total{entity_type,backend,result}
    kept     — LLM said "yes, real PII"
    reverted — LLM said "no, false positive"
    forwarded — pattern type, no LLM review
pb_ingestion_pii_verifier_duration_seconds{backend}
```

### Trace

Every verifier call emits an OTel span named `pii_verify`
(service `ingestion`, attributes `backend`, `input_count`). Shows up
in Tempo/Grafana alongside the existing `extract` / `scan` /
`quality` / `privacy` spans.

### Demo panel

`demo/panels/pipeline_inspector.py` renders a dedicated section
`2b · Semantic Verifier` when enabled. Counts are visible per
entity type: input, forwarded, reviewed, kept, reverted, duration.

## Graceful degradation

| Failure | Behaviour |
|---|---|
| OPA unreachable | Fall back to env-var defaults (community: noop). No blocking. |
| LLM unreachable | Keep all Presidio hits; log `pii_verify_provider … failed` warning; increment `errors` stat. |
| LLM returns malformed JSON | Same as unreachable (logged, errors++, keep all). |
| LLM returns partial verdicts | Missing indices default to `True` (keep). |
| Verifier provider misconfigured (missing URL/model for `backend=llm`) | Factory emits a warning and returns the noop provider. Ingestion never crashes. |

The contract is deliberate: **at no point can the verifier reduce
recall**. It can only *remove* candidates Presidio generated; it
never adds hits, never re-labels, never changes spans.

## Compliance posture

* Only the candidate value + ±60-char context window leaves the
  ingestion service. The full document is never sent to the LLM.
* The decision (`kept` / `reverted`) is logged per entity type in
  Prometheus. No per-document verdict trail is stored — if auditors
  need it, enable OTel span export to your trace store.
* Audit chain integrity (Art. 12) is unaffected — the verifier
  runs before vault storage and classification decisions, so the
  audit trail of vault accesses remains intact.

## Testing

```
shared/tests/test_pii_verify_provider.py    # provider contract + fail-open
ingestion/tests/test_preview_endpoint.py    # /preview integration
```

Live verification:

```bash
# Enable the verifier via manage_policies:
curl -sX POST http://localhost:8080/mcp \
  -H "Authorization: Bearer pb_admin_key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
    "name":"manage_policies",
    "arguments":{"action":"update","section":"ingestion","data":{
       "min_quality_score": {"default": 0.6, "github": 0.3},
       "duplicate_threshold": 0.95,
       "pii_verifier": {"enabled": true, "backend": "llm",
                        "min_confidence_keep": 0.5}}}}}'

# Then preview a NovaTech customer fixture:
curl -sX POST http://localhost:8081/preview \
  -H "Content-Type: application/json" \
  -d @demo/fixtures/sharepoint_rahmenvertrag.md \
  | jq '.verifier'
```

## Related work

See [docs/pii-custom-model.md](pii-custom-model.md) for the longer-
horizon plan — when and why a fine-tuned German PII model would
replace the LLM verifier.
