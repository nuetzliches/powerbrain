# Incident MCP Tools — Spec (B-47)

**Status:** approved 2026-05-12, in implementation
**Owner:** Powerbrain core team
**Related:** [init-db/006_privacy_incidents.sql](../../init-db/006_privacy_incidents.sql), [docs/plans/2026-04-08-eu-ai-act-compliance.md](../plans/2026-04-08-eu-ai-act-compliance.md), [docs/compliance-claude-desktop.md](../compliance-claude-desktop.md)

## Motivation

The `privacy_incidents` schema (migration `006_privacy_incidents.sql`) has been in place since the EU AI Act compliance push but has had no MCP-tool surface. Detection paths (PII scanner, retention check, manual audit) had no programmatic way to *report* a finding, and the DPO had no programmatic way to *assess* / *notify* / *resolve*. This spec adds five MCP tools plus a worker-job watching the 72-hour Art. 33 deadline.

Goal: every step of the GDPR Art. 33/34 evidence chain has a tool. The actual outbound notification (email to authority, letter to subject) remains an organisational process — Powerbrain documents what was decided and when, not how it was sent.

## Scope

In scope:
- 5 MCP tools (`report_breach`, `list_incidents`, `assess_incident`, `notify_authority`, `notify_data_subject`)
- 1 worker job (`incident_deadline_check`) — Prometheus metrics + alert rule
- OPA policy package `pb.incidents` with RBAC + configurable risk-score weights
- Unit tests (MCP-side + OPA-side)
- Doc updates: `docs/mcp-tools.md`, `CLAUDE.md`, `CHANGELOG.md`

Out of scope (deliberately):
- DPO role as separate `agent_role` — admin-only for now; DPO role can be added in a follow-up migration that widens `agent_role` CHECK
- Outbound notification transport (email to authority, letter to subject) — kept as organisational workflow
- Auto-discovery of the right supervisory authority — DPO decides per case
- Linking to `deletion_requests` (column already exists but is not bound in MVP)
- Automatic phased re-notification scheduling (Art. 33(4))

## Tools

### 1. `report_breach`

Creates a new `privacy_incidents` row with `status='detected'`.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `description` | string | yes | Free text of what was found |
| `source` | enum | yes | One of `llm_detection`, `pii_scanner`, `agent_report`, `manual_audit`, `retention_check` |
| `affected_data` | object | no | JSONB blob — dataset_ids, document_ids, qdrant_point_ids, etc. |
| `pii_types_found` | array | no | Presidio entity types — used by risk-scoring |
| `data_category` | string | no | One of the `data_categories` ids (customer_data, employee_data, …) |
| `estimated_subjects` | integer | no | Number of affected subjects when exact mapping unavailable |
| `data_subject_ids` | array of UUID | no | Exact references if known |

Returns: `{incident_id, detected_at, status: "detected"}`.

**RBAC:** Any authenticated role including `viewer`. Rationale: detection suppression is a worse failure than over-reporting. Agents that find PII while answering queries must be able to flag it.

**Audit:** Logs to `agent_access_log` with action=`incident_report`.

### 2. `list_incidents`

Returns incidents with optional filters. Admin-only.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `status` | string | no | Filter by single status |
| `source` | string | no | Filter by source |
| `attention` | boolean | no | If `true`, returns `v_incidents_requiring_attention` rows only (open + 72h deadline view) |
| `limit` | integer | no | Default 50, max 500 |

Returns: array of incident records. When `attention=true`, includes `hours_since_detection` and `frist_warnung` from the view.

### 3. `assess_incident`

Computes `notifiable_risk` based on OPA-driven scoring, updates incident.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `incident_id` | UUID | yes | |
| `risk_assessment` | string | no | Free-text rationale; if omitted, generated from the score breakdown |
| `force_notifiable` | boolean | no | Admin override — set `notifiable_risk=true` regardless of score (e.g. judgment call) |
| `force_not_notifiable` | boolean | no | Admin override — set `notifiable_risk=false` (with explicit rationale required) |

Behaviour:
- Score = sum(weight per unique `pii_types_found`) × subject-count-multiplier × category-multiplier
- Weights, multipliers, threshold come from OPA `data.pb.config.incidents.risk_score`
- If score ≥ threshold OR `force_notifiable=true` → `notifiable_risk=true`, status→`under_review`
- If score < threshold AND `force_not_notifiable=true` → `notifiable_risk=false`, status→`false_positive` (requires `risk_assessment` text)
- Otherwise → `notifiable_risk` set, status→`under_review` (DPO still must decide whether to notify)

Returns: `{incident_id, risk_score, breakdown, notifiable_risk, status}`.

### 4. `notify_authority`

Records that Art. 33 notification has been sent. Powerbrain does **not** send the notification itself.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `incident_id` | UUID | yes | |
| `authority_name` | string | yes | e.g. "LfDI BW", "BfDI", "BayLDA" |
| `authority_ref` | string | no | Authority's reference number / ticket id |
| `notification_method` | string | no | One of `online_portal`, `email`, `letter`, `phone_documented_followup` |
| `notified_at` | string (ISO-8601) | no | Default: now() |
| `notes` | string | no | Free text — e.g. attached files, copy of submission |

Updates `authority_notified_at`, `authority_ref`, status → `notified_authority`.

**RBAC:** Admin only.

### 5. `notify_data_subject`

Records that Art. 34 notification has been sent to a data subject.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `incident_id` | UUID | yes | |
| `subject_ref` | string | yes | UUID from `data_subjects` or external reference (e.g. account id) |
| `channel` | string | yes | One of `email`, `letter`, `in_app`, `phone_documented_followup` |
| `template_id` | string | no | Reference to a template document if one was used |
| `notified_at` | string (ISO-8601) | no | Default: now() |
| `notes` | string | no | Free text |

If multiple subjects are notified individually, the tool can be called once per subject (status only moves to `notified_subject` on the first call; subsequent calls append to a `subject_notifications` JSONB array — addition to schema needed: column `subject_notifications JSONB DEFAULT '[]'::jsonb`).

Decision for MVP: only mark the *status* transition; multi-subject ledger is out of scope. The single `subject_notified_at` records the first call. Future enhancement.

**RBAC:** Admin only.

## OPA policy

Package `pb.incidents`. New file `opa-policies/pb/incidents.rego`.

Roles:
- `allow_report` — any authenticated role
- `allow_list` — admin
- `allow_assess` — admin
- `allow_notify_authority` — admin
- `allow_notify_subject` — admin

Risk scoring (`risk_score(pii_types, estimated_subjects, data_category) → number`):

```rego
package pb.incidents

import future.keywords

default risk_score := 0

# Weight per unique PII type
type_weights := data.pb.config.incidents.risk_score.weights

high_types := {t | t := data.pb.config.incidents.risk_score.high_pii_types[_]}
medium_types := {t | t := data.pb.config.incidents.risk_score.medium_pii_types[_]}
low_types := {t | t := data.pb.config.incidents.risk_score.low_pii_types[_]}

base_score(types) := s {
    high_hits := {t | t := types[_]; high_types[t]}
    med_hits  := {t | t := types[_]; medium_types[t]}
    low_hits  := {t | t := types[_]; low_types[t]}
    s := count(high_hits) * type_weights.high
       + count(med_hits)  * type_weights.medium
       + count(low_hits)  * type_weights.low
}

subject_multiplier(n) := m {
    # Select the highest threshold the count exceeds
    candidates := [t.multiplier | t := data.pb.config.incidents.risk_score.subject_multipliers[_]; n >= t.min]
    m := max(candidates)
}

category_multiplier(cat) := 1.0 { not cat == "restricted"; not cat == "confidential" }
category_multiplier("restricted")   := data.pb.config.incidents.risk_score.category_multiplier_restricted
category_multiplier("confidential") := data.pb.config.incidents.risk_score.category_multiplier_confidential

risk_score := s {
    base := base_score(input.pii_types)
    s    := base * subject_multiplier(input.subjects) * category_multiplier(input.data_category)
}

notifiable_threshold := data.pb.config.incidents.risk_score.notifiable_threshold
```

Data section in `opa-policies/data.json`:

```json
"incidents": {
  "risk_score": {
    "high_pii_types":   ["EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "US_SSN",
                         "MEDICAL_LICENSE", "CREDIT_CARD"],
    "medium_pii_types": ["PERSON", "DATE_OF_BIRTH", "LOCATION"],
    "low_pii_types":    ["ORG", "URL", "NRP"],
    "weights":          {"high": 30, "medium": 15, "low": 5},
    "subject_multipliers": [
      {"min": 1,    "multiplier": 1.0},
      {"min": 10,   "multiplier": 2.0},
      {"min": 100,  "multiplier": 4.0},
      {"min": 1000, "multiplier": 8.0}
    ],
    "category_multiplier_restricted":   2.0,
    "category_multiplier_confidential": 1.5,
    "notifiable_threshold": 50
  },
  "roles": {
    "allow_report":          ["viewer", "analyst", "developer", "admin"],
    "allow_list":            ["admin"],
    "allow_assess":          ["admin"],
    "allow_notify_authority":["admin"],
    "allow_notify_subject":  ["admin"]
  }
}
```

JSON Schema extension added accordingly.

## Worker job: `incident_deadline_check`

File: `worker/jobs/incident_deadline_check.py`.

Behaviour:
- Schedule: every 15 minutes (frequent enough to give meaningful Prometheus resolution, light enough to be cheap)
- Queries `v_incidents_requiring_attention`
- Sets gauges:
  - `pb_incidents_open_total{status=...}` — number of incidents per status
  - `pb_incidents_attention_total{severity=...}` — 0..n in severity buckets `warning`, `critical`, `overdue`
  - `pb_incidents_oldest_open_hours` — hours since detection for oldest open incident
- Counter `pb_incidents_overdue_seen_total{incident_id}` — emitted once per overdue incident per scheduler tick (so Alertmanager can detect "this incident has been overdue for X consecutive ticks")
- Structured logs at WARNING/CRITICAL level for ops dashboards

Alert rule additions in `monitoring/alerting_rules.yml`:

```yaml
- alert: IncidentNotificationDeadlineImminent
  expr: pb_incidents_attention_total{severity="critical"} > 0
  for: 5m
  labels: { severity: critical, area: gdpr }
  annotations:
    summary: "GDPR Art. 33 notification deadline within 24 hours"
    description: "At least one privacy incident has been open for >48h without notification. Review via list_incidents attention=true."

- alert: IncidentNotificationOverdue
  expr: pb_incidents_attention_total{severity="overdue"} > 0
  for: 5m
  labels: { severity: page, area: gdpr }
  annotations:
    summary: "GDPR Art. 33 notification overdue"
    description: "Privacy incident open >72h without notification. Notify supervisory authority immediately or document delay reason."
```

The view's `frist_warnung` text is mapped to severities:
- "CRITICAL: less than 24h until the 72h deadline" → `critical`
- "WARNING: incident not yet assessed" → `warning`
- `>72h` and not notified → `overdue` (new bucket, computed in worker since view does not produce it explicitly)

## Tests

`mcp-server/tests/test_incident_tools.py`:
- `report_breach` from each role inserts a row; missing required args → 400; status='detected' after insert
- `list_incidents` with various filters; attention=true → uses the view
- `assess_incident` correctly computes score; admin override paths; missing rationale on `force_not_notifiable` → error
- `notify_authority` admin-only; updates status correctly; refuses if incident is in wrong state
- `notify_data_subject` admin-only; updates status; idempotent for subsequent calls (still records first `notified_at`)

`opa-policies/pb/test_incidents.rego`:
- RBAC matrix per tool
- Risk-score boundary cases (no PII, single low PII, multiple high PII, restricted category multiplier, subject-count brackets)
- `notifiable_threshold` exactly at the boundary

## Acceptance

- `docker exec pb-opa opa test /policies/ -v` passes including new tests
- `python -m pytest mcp-server/tests/test_incident_tools.py -v` passes
- `list_incidents attention=true` returns proper data for a seeded overdue incident
- Worker job appears in scheduler logs at startup; metrics scrape successful
- Alert rule renders in `promtool check rules monitoring/alerting_rules.yml`
