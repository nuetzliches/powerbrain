# PII Scan Observability & Strict Defaults

**Status**: Done
**Priority**: High
**Created**: 2026-03-27
**Context**: Demo testing revealed that the PII scan status is not distinguishable in logs

## Problem

During the demo UI evaluation it was discovered that one cannot reliably distinguish in the logs and telemetry whether a chat message was:

1. **PII-scanned and clean** (no entities found)
2. **PII-scanned and pseudonymized** (entities replaced)
3. **PII scan failed**, but request continued anyway (fail open)
4. **PII scan disabled** by policy

### Current State

- `pii_pseudonymize` telemetry step exists (after upstream rebuild), shows `status: ok`
- But: no difference between "clean" and "pseudonymized" in telemetry
- `PII_SCAN_FORCED` defaults to `false` → fail open on scanner outage
- OPA policy `pb/proxy` has `pii_scan_forced` only as an opt-in flag, not as a default

### Security Risk

With strict default policies, a request should be **blocked** (503) if the PII scanner is unreachable. Currently the request is forwarded to the LLM provider without PII protection.

## Requirements

### 1. Extend the PII telemetry step

Extend `pii_pseudonymize` step metadata:

```json
{
  "name": "pii_pseudonymize",
  "service": "pb-proxy",
  "ms": 7.28,
  "status": "ok",
  "metadata": {
    "mode": "enabled|forced|disabled",
    "entities_found": 3,
    "entities_pseudonymized": 3,
    "entity_types": ["PERSON", "EMAIL_ADDRESS"],
    "fail_mode": null
  }
}
```

Possible status values:
- `ok` — scan successful, possibly with entities
- `skipped` — scan disabled by policy
- `fail_open` — scan failed, request continued
- `fail_closed` — scan failed, request blocked (503)

### 2. Improve logging

After each PII scan, an explicit log line:

```
INFO [pb-proxy] PII scan: status=ok, mode=forced, entities_found=0
INFO [pb-proxy] PII scan: status=ok, mode=enabled, entities_found=3, types=[PERSON, EMAIL_ADDRESS, PHONE_NUMBER]
WARNING [pb-proxy] PII scan: status=fail_open, mode=enabled, error="ingestion service unreachable"
ERROR [pb-proxy] PII scan: status=fail_closed, mode=forced, error="ingestion service unreachable"
INFO [pb-proxy] PII scan: status=skipped, mode=disabled (admin opt-out)
```

### 3. Tighten default: `pii_scan_forced` → true

**Option A**: Change env var default in `config.py`:
```python
PII_SCAN_FORCED = os.getenv("PII_SCAN_FORCED", "true").lower() == "true"
```

**Option B**: Change OPA policy default:
```rego
default pii_scan_forced := true

pii_scan_forced := false if {
    input.agent_role == "admin"
    input.pii_scan_forced_override == false
}
```

Recommendation: **Option A + B combined** — config default strict, OPA can override.

## Affected Files

- `pb-proxy/proxy.py` — PII scan block (~lines 463-516)
- `pb-proxy/config.py` — `PII_SCAN_FORCED` default
- OPA policy `pb/proxy` — `pii_scan_forced` rule

## Test Plan

1. Chat with PII content (name + email) → telemetry shows `entities_found: 2, status: ok`
2. Chat without PII → telemetry shows `entities_found: 0, status: ok`
3. Stop ingestion service, send chat → 503 (fail closed)
4. Admin role with opt-out → telemetry shows `status: skipped`
5. `PII_SCAN_FORCED=false` → warning log on scanner outage, request passes through
