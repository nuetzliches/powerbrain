# Powerbrain Backlog

Offene Aufgaben, priorisiert. Aktualisiert: 2026-03-27.

---

## Backlog — Policy Management Roadmap

### B-10: OPAL Integration (Option B)
**Priorität:** Niedrig (nach B-01)
**Aufwand:** ~2–3 Tage

OPAL für Realtime Policy+Data Sync statt OPA Bundle-Polling.

- [ ] OPAL Server + Client als Docker-Services
- [ ] Git-Watcher auf Forgejo `pb-policies` Repo
- [ ] WebSocket-basierter Push bei Policy-Änderungen

### B-11: Policy Management Web-UI (Option C)
**Priorität:** Niedrig (nach B-01, optional nach B-10)
**Aufwand:** ~4–5 Tage

Leichtgewichtiges Web-Frontend für Policy-Data-Verwaltung.

- [ ] Policy-Data Editor (JSON-Formulare)
- [ ] JSON-Schema-Validierung im Frontend
- [ ] OPA Dry-Run / Policy Preview
- [ ] Commit nach Forgejo (Versioning + Audit Trail)
- [ ] Rollen-basierter Zugriff (Admin only)

### B-12: MCP-Tool `manage_policies` (CRUD)
**Priorität:** Mittel
**Aufwand:** ~1 Tag

MCP-Tool zum Lesen/Schreiben von Policy-Daten via OPA Data API.

- [ ] `manage_policies` Tool: read/update policy data sections
- [ ] JSON-Schema-Validierung vor Schreibzugriff
- [ ] OPA-Admin-only Zugriffskontrolle

---

## Backlog — Technische Schulden

### B-20: PipelineStep-Mock in proxy.py aufräumen
**Priorität:** Niedrig
**Aufwand:** ~0.5 Tag

Der `except ImportError`-Fallback definiert einen eigenen `PipelineStep`, der vom Original in `shared/telemetry.py` divergieren kann.

### B-21: Forgejo Workflows → internes Infra-Repo
**Priorität:** Mittel (Pre-Public)
**Aufwand:** ~0.5 Tag

`.forgejo/workflows/` enthält interne Runner-Namen und Registry-URLs. Vor Open-Sourcing in internes Repo verschieben.

### B-22: GitHub Actions CI (Pre-Public)
**Priorität:** Mittel (Pre-Public)
**Aufwand:** ~1 Tag

Generische GitHub Actions als Ersatz für Forgejo-spezifische Workflows.

### B-23: Secrets/URLs Audit (Pre-Public)
**Priorität:** Hoch (Pre-Public)
**Aufwand:** ~0.5 Tag

Grep nach `nuetzliche.it`, `baumeister`, internen IPs. Parameterisieren oder entfernen.

### B-24: LICENSE-Datei hinzufügen
**Priorität:** Hoch (Pre-Public)
**Aufwand:** ~5 Min

---

## Erledigt

### ✅ B-01: OPA Policy-Data Extraktion (2026-03-27)
Alle Business-Daten aus 5 Rego-Dateien in `opa-policies/pb/data.json` extrahiert.
Rego enthält nur noch Logik. JSON-Schema-Validierung. 85 OPA-Tests (vorher: 33).

### ✅ B-02: E2E Smoke Tests für pb-proxy (2026-03-27)
`tests/integration/e2e/test_proxy_smoke.py` — Auth, OPA-Policy, Tool-Injection, Health/Models/Metrics.

### ✅ B-03: Reranker Provider Integrationstest (2026-03-27)
`mcp-server/tests/test_reranker_integration.py` — 7 Tests für Provider-Wechsel (powerbrain/tei/cohere), Graceful Fallback bei Timeout/Connection-Error/500.

Siehe auch `docs/KNOWN_ISSUES.md` für alle gelösten Issues (Sprints 1–5).
Siehe `docs/plans/` für abgeschlossene Feature-Implementierungen.
