# PII Scan Observability & Strict Defaults

**Status**: Backlog
**Priority**: High
**Created**: 2026-03-27
**Context**: Demo-Testing ergab, dass PII-Scan-Status in Logs nicht unterscheidbar ist

## Problem

Bei der Demo-UI-Evaluierung wurde festgestellt, dass man im Log und in der Telemetry nicht zuverlässig unterscheiden kann, ob eine Chat-Nachricht:

1. **PII-gescannt und sauber** war (keine Entities gefunden)
2. **PII-gescannt und pseudonymisiert** wurde (Entities ersetzt)
3. **PII-Scan fehlgeschlagen**, aber Request trotzdem fortgesetzt wurde (fail open)
4. **PII-Scan deaktiviert** per Policy

### Aktueller Zustand

- `pii_pseudonymize` Telemetry-Step existiert (nach upstream-Rebuild), zeigt `status: ok`
- Aber: kein Unterschied zwischen "clean" und "pseudonymized" im Telemetry
- `PII_SCAN_FORCED` ist Default `false` → fail open bei Scanner-Ausfall
- OPA-Policy `pb/proxy` hat `pii_scan_forced` nur als opt-in Flag, nicht als Default

### Sicherheitsrisiko

Bei strengen Default-Policies sollte ein Request **blockiert** werden (503), wenn der PII-Scanner nicht erreichbar ist. Aktuell wird der Request ohne PII-Schutz an den LLM-Provider weitergeleitet.

## Anforderungen

### 1. PII-Telemetry-Step erweitern

`pii_pseudonymize` Step Metadata erweitern:

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

Mögliche Status-Werte:
- `ok` — Scan erfolgreich, ggf. mit Entities
- `skipped` — Scan per Policy deaktiviert
- `fail_open` — Scan fehlgeschlagen, Request fortgesetzt
- `fail_closed` — Scan fehlgeschlagen, Request blockiert (503)

### 2. Logging verbessern

Nach jedem PII-Scan eine explizite Log-Zeile:

```
INFO [pb-proxy] PII scan: status=ok, mode=forced, entities_found=0
INFO [pb-proxy] PII scan: status=ok, mode=enabled, entities_found=3, types=[PERSON, EMAIL_ADDRESS, PHONE_NUMBER]
WARNING [pb-proxy] PII scan: status=fail_open, mode=enabled, error="ingestion service unreachable"
ERROR [pb-proxy] PII scan: status=fail_closed, mode=forced, error="ingestion service unreachable"
INFO [pb-proxy] PII scan: status=skipped, mode=disabled (admin opt-out)
```

### 3. Default verschärfen: `pii_scan_forced` → true

**Option A**: Env-Var Default ändern in `config.py`:
```python
PII_SCAN_FORCED = os.getenv("PII_SCAN_FORCED", "true").lower() == "true"
```

**Option B**: OPA-Policy Default ändern:
```rego
default pii_scan_forced := true

pii_scan_forced := false if {
    input.agent_role == "admin"
    input.pii_scan_forced_override == false
}
```

Empfehlung: **Option A + B kombiniert** — Config-Default strict, OPA kann übersteuern.

## Betroffene Dateien

- `pb-proxy/proxy.py` — PII-Scan Block (~Zeile 463-516)
- `pb-proxy/config.py` — `PII_SCAN_FORCED` Default
- OPA-Policy `pb/proxy` — `pii_scan_forced` Rule

## Testplan

1. Chat mit PII-Content (Name + E-Mail) → Telemetry zeigt `entities_found: 2, status: ok`
2. Chat ohne PII → Telemetry zeigt `entities_found: 0, status: ok`
3. Ingestion-Service stoppen, Chat senden → 503 (fail closed)
4. Admin-Rolle mit opt-out → Telemetry zeigt `status: skipped`
5. `PII_SCAN_FORCED=false` → Warning-Log bei Scanner-Ausfall, Request geht durch
