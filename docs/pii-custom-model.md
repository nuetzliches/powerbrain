# PII Custom Model — Long-horizon Roadmap

> **Status:** Design note. Not planned for v0.x.
> **Supersedes:** Parts of the LLM verifier when adopted — see
> [pii-verifier.md](pii-verifier.md) for the current approach.

This doc captures *when* and *how* Powerbrain would replace the
Presidio-plus-LLM-verifier pipeline with a purpose-trained PII
detector. It exists so we have a clear trigger for the decision and
a shared understanding of what the project would actually entail —
not so we start tomorrow.

## The case for a custom model

The LLM verifier solves today's precision problem elegantly, but
inherits three long-run liabilities:

1. **Latency.** Even the fastest local models (qwen2.5:3b on
   modern CPU) land around 250–400 ms per batch. At ingestion
   throughputs above ~50k documents/month this compounds into
   real worker hours and real GPU bills.
2. **Determinism via prompt.** Prompt engineering makes the
   outcome reproducible *until* the underlying model is replaced
   behind the same endpoint. A silent upgrade of `qwen2.5` to
   `qwen3` can shift our false-positive rate without anyone
   noticing.
3. **External dependency.** Every deployment that opts in gains a
   hard runtime dependency on an LLM endpoint. For the strictest
   regulated sectors (defence, some healthcare verticals) that's a
   non-starter even when the model is local.

A purpose-built transformer encoder (BERT/DeBERTa/XLM-R family)
fine-tuned on German PII detection solves all three: 5–20 ms per
document on CPU, deterministic across deployments, no LLM
endpoint required.

## When to trigger this

Adopt a custom model when **any** of these are true:

| Trigger | Threshold | Signal |
|---|---|---|
| **Throughput** | > 50 000 docs/month on a single tenant | `pb_ingestion_requests_total{endpoint="ingest"}` over 30 days |
| **Latency SLA** | p99 sub-200 ms budget for PII scan | Product requirement / customer contract |
| **Zero-LLM requirement** | Regulated sector bans LLM at ingestion path | Customer security questionnaire |
| **Drift of LLM verifier** | >5 % month-over-month change in `reverted/reviewed` ratio without policy change | Grafana alert on `pb_ingestion_pii_verifier_calls_total` |
| **Language expansion** | Three or more languages needed (beyond DE/EN) | Adapter roadmap |

If fewer than two triggers are lit, the LLM verifier is the right
answer. Building and operating an in-house ML stack is a
significant commitment — quantify the payoff first.

## What "good" looks like

### Target architecture

```
  Text
    ↓
  ┌───────────────────────────────────────────────┐
  │  Custom DE PII model                          │   ← Replaces both Presidio
  │  • transformer encoder, ~250M params          │     AND the LLM verifier
  │  • BIO-tagged token classification            │
  │  • threshold-tuned per entity type            │
  └───────────────────────────────────────────────┘
             │ entity_locations[] (already precision-filtered)
             ↓
  quality gate → OPA privacy → pseudonymise → vault
```

The custom model delivers **both** recall (what Presidio did)
**and** precision (what the LLM verifier does). The two existing
layers collapse into one deterministic step.

### Non-goals

* Not a general-purpose NER. We only train for the exact entity
  taxonomy Powerbrain uses (`PERSON`, `LOCATION`, `EMAIL_ADDRESS`,
  etc.). No `ORGANIZATION` / `MISC` / `PRODUCT` unless they become
  policy-relevant.
* Not a replacement for pattern-based regex recognizers (IBAN,
  phone, DOB). Those stay — they're free precision.
* Not continuously online-learning. Periodic batch retraining with
  a shipping cadence (quarterly at most).

## The work — grouped by phase

### Phase 1 · Data

**Probably the hardest part.** Public German PII datasets are
sparse (GermanNER, WikiNER-DE are general NER, not PII; GermEval
2014 is closer but still generic).

Strategy:

1. **Bootstrap with the LLM verifier's decisions.** Every
   `reverted` decision in `pb_ingestion_pii_verifier_calls_total`
   is a labelled negative. Every `kept` decision is a labelled
   positive. Six months of real traffic gives us the foundation.
2. **Augment with the NovaTech fixtures + partner data.** Fixture
   documents (`demo/fixtures/*`) and any customer-provided
   anonymised samples seed the positive class for rare entity
   types (DE_TAX_ID, DE_DATE_OF_BIRTH).
3. **Synthetic hard negatives.** German business boilerplate
   ("Geschäftsführer", "Rahmenvertrag", "Sparkasse Köln",
   department names, job titles). These are cheap to generate from
   curated word lists.
4. **Active learning loop.** Uncertain model predictions get
   reviewed by admins through a dedicated MCP tool. Labelled rows
   feed the next training run.

Target dataset size: ~50k annotated sentences, balanced. Achievable
in two to three months if the bootstrap step runs in parallel with
live LLM-verifier traffic.

### Phase 2 · Model

| Decision | Recommendation | Rationale |
|---|---|---|
| Base model | `xlm-roberta-large` or `deepset/gbert-large` | Strong German baseline, public weights, no licensing surprises |
| Architecture | Token classification (BIO) head | Matches Presidio's span output format — downstream code unchanged |
| Training budget | ~$200–500 on a single A100 run | 3 epochs, batch 16, seq_len 256 |
| Evaluation | F1 per entity type, **recall prioritised** | Public dataset split + held-out NovaTech fixtures + held-out customer-donated set |
| Quantisation | INT8 via Optimum/ONNX for CPU inference | Target <20 ms per doc on ingestion container CPU |

### Phase 3 · Serving

Add a new backend to the existing `pii_verify_provider.py`
abstraction:

```python
create_pii_verify_provider(backend="custom_model",
                           model_path="/models/pb-pii-de-v1.onnx")
```

No API change, no call-site change in ingestion. The custom model
lives in a dedicated lightweight service (or runs in-process in
the ingestion container via ONNX Runtime), exposes the same
`verify()` contract, and is picked up by setting
`PII_VERIFIER_BACKEND=custom_model`.

The abstraction from [pii-verifier.md](pii-verifier.md) is
deliberately written to make this swap clean.

### Phase 4 · Operations

New concerns that arrive with in-house ML:

* **Model registry.** Store ONNX artefacts with version + commit
  hash + training-dataset digest. Candidate: MLflow / lightweight
  S3-backed registry. Match the deployment tool already in use.
* **Drift monitoring.** Compare the model's F1 on a static
  held-out set against freshly labelled traffic samples weekly.
  Alert on > 5% degradation.
* **Retraining cadence.** Quarterly by default, manually
  triggerable when a new deployment contributes substantial new
  labelled data.
* **Rollback.** The custom-model backend must fall back to the
  LLM verifier, not to raw Presidio. The LLM verifier stays in
  the codebase permanently as the safe-mode PII layer.
* **CI.** A new test gate: the trained artefact must clear a
  minimum F1 threshold on the held-out fixture set before the
  release workflow accepts it.

### Phase 5 · Documentation + audit

* EU AI Act Art. 10 data quality: the training corpus becomes
  part of the compliance documentation. `generate_compliance_doc`
  is extended to include the current model's training-set digest
  and evaluation metrics.
* Art. 15 drift monitoring: the retraining cadence and drift-
  alert thresholds land in `docs/risk-management.md` as a new
  risk row.

## Rough effort estimate

These are not commitments — they're fingers-in-the-air estimates
to help the decision:

| Phase | Effort | Dependencies |
|---|---|---|
| 1 · Data | 2–3 months | 6 months of LLM-verifier traffic to bootstrap |
| 2 · Model | 3–4 weeks | Data from phase 1, a single A100 run budget |
| 3 · Serving | 1 week | Existing verifier abstraction |
| 4 · Operations | 2–3 weeks | Model registry choice, drift dashboard |
| 5 · Docs + audit | 1 week | Model evaluation metrics locked in |

Total: 4–5 months elapsed, 1 dedicated ML engineer + fractional
MLOps / security reviewer.

## Why not sooner

Four reasons we're deliberately not starting:

1. **Demand unproven.** Today Powerbrain is a context engine
   shipped as an Apache-2.0 stack. The number of deployments that
   need sub-200 ms PII is plausibly zero. The LLM verifier covers
   the precision problem well enough until that changes.
2. **Data scarcity is a real blocker.** The bootstrap from LLM
   verifier decisions only pays off after months of real traffic.
   Starting sooner means training on synthetic data, which
   historically produces brittle models.
3. **MLOps tax.** Operating any in-house model requires a drift
   story, retraining pipeline, audit documentation. None of that
   exists in Powerbrain today. Adding it prematurely eats
   velocity we'd rather spend on adapters (Slack, Jira,
   Confluence) that move the product forward.
4. **The verifier is the right abstraction to keep.** When we do
   train a custom model, it plugs into the same
   `pii_verify_provider` factory. Nothing that exists today gets
   thrown away.

## Review cadence

Revisit this decision quarterly. Specifically review:

1. The triggers table above — any lit?
2. The Grafana dashboards for `pii_verifier` — how large is the
   `reverted` rate over time, and is it stable?
3. Customer pipeline: any mention of a sub-200 ms SLA in the last
   10 customer conversations?

If all three are still negative, defer another quarter.

## Related reading

* [pii-verifier.md](pii-verifier.md) — the current approach
* [architecture.md](architecture.md) — where PII sits in the pipeline
* [risk-management.md](risk-management.md) — Art. 9 risk register
