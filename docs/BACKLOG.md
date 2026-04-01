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

## Backlog — Reranking

### B-13: boost_corrections — Korrektur-Dokumente im Reranking bevorzugen
**Priorität:** Niedrig
**Aufwand:** ~0.5 Tag

timecockpit-mcp speichert benutzerkorrigierte Zeiteintrags-Beschreibungen als Dokumente mit
`metadata.isCorrection: true` in der KB (source_type `timesheet`). Diese repräsentieren
validierte, qualitativ hochwertige Texte und sollten bei Ähnlichkeitssuchen bevorzugt werden.

Neuer Reranking-Parameter `boost_corrections` (analog zu `boost_same_author`):
- Heuristic Boost auf Dokumente mit `metadata.isCorrection == true`
- Empfohlener Default-Boost: 0.1–0.2
- Konfigurierbar über `rerank_options` im `search_knowledge`-Aufruf

- [ ] `boost_corrections` Parameter in Reranking-Pipeline implementieren
- [ ] Metadata-Field `isCorrection` in Scoring berücksichtigen
- [ ] Tests: Korrektur-Dokument wird höher gerankt als identischer Text ohne Flag

---

## Backlog — PII & Datenschutz

### B-30: graph_query liefert ungescannte Klarnamen
**Priorität:** Mittel
**Aufwand:** ~0.5–1 Tag

`graph_query` liefert Knowledge-Graph-Knoten aus Apache AGE, die beim Import mit Klarnamen angelegt wurden (User-Nodes mit `firstname`, `lastname`, `email`). Diese Daten wurden nicht durch die PII-Ingestion-Pipeline pseudonymisiert. Powerbrain ist als `pii_status: scanned` deklariert, was für `graph_query` streng genommen nicht zutrifft.

**Optionen:**
- [ ] A: Powerbrain auf `pii_status: mixed` umstellen und `graph_query` als unscanned deklarieren
- [ ] B: Graph-Knoten-Properties beim Import pseudonymisieren (analog zu Qdrant Dual Storage)
- [ ] C: `graph_query` Results im MCP-Server pseudonymisieren bevor sie zurückgegeben werden

### B-31: Ingestion pseudonymisiert Metadaten nicht
**Priorität:** Mittel
**Aufwand:** ~1 Tag

Die Ingestion-Pipeline scannt und pseudonymisiert den `source`-Text (der embedded wird), aber das `metadata`-Objekt bleibt unverändert. Import-Scripts schreiben dort Klarnamen (`userName`, `customerName`, `authorEmail`). Diese Metadaten landen ungescannt in Qdrant-Payload und PostgreSQL und werden bei `search_knowledge`-Results mitgeliefert.

- [ ] PII-Scan auf konfigurierbare Metadaten-Felder ausweiten
- [ ] Oder: Metadaten-Felder mit PII bei der Ausgabe filtern (OPA `fields_to_redact`)

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

---

## Erledigt

### ✅ B-01: OPA Policy-Data Extraktion (2026-03-27)
Alle Business-Daten aus 5 Rego-Dateien in `opa-policies/pb/data.json` extrahiert.
Rego enthält nur noch Logik. JSON-Schema-Validierung. 85 OPA-Tests (vorher: 33).

### ✅ B-02: E2E Smoke Tests für pb-proxy (2026-03-27)
`tests/integration/e2e/test_proxy_smoke.py` — Auth, OPA-Policy, Tool-Injection, Health/Models/Metrics.

### ✅ B-23: Secrets/URLs Audit (2026-03-27)
`build-images.sh` parameterisiert (REGISTRY als Required-Var), Doku-Pfade bereinigt, `.env.example` verifiziert.

### ✅ B-24: LICENSE-Datei (2026-03-27)
Apache License 2.0 hinzugefügt.

### ✅ B-03: Reranker Provider Integrationstest (2026-03-27)
`mcp-server/tests/test_reranker_integration.py` — 7 Tests für Provider-Wechsel (powerbrain/tei/cohere), Graceful Fallback bei Timeout/Connection-Error/500.

Siehe auch `docs/KNOWN_ISSUES.md` für alle gelösten Issues (Sprints 1–5).
Siehe `docs/plans/` für abgeschlossene Feature-Implementierungen.
