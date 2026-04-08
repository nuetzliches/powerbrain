# Powerbrain Backlog

Offene Aufgaben, priorisiert. Aktualisiert: 2026-04-08.

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

## Backlog — EU AI Act Compliance (August 2026)

Anforderungen aus Art. 9–15 EU AI Act für High-Risk AI Systeme.
Powerbrain ist selbst kein High-Risk-System, aber Deployer in regulierten Branchen
(Finanz, Gesundheit, HR) brauchen diese Fähigkeiten von ihrer Context-Infrastruktur.

### B-40: Tamper-Resistant Audit Logs (Art. 12 Record-Keeping)
**Priorität:** Hoch
**Aufwand:** ~2 Tage
**EU AI Act:** Art. 12 — automatische, manipulationssichere Protokollierung

Aktueller Stand: Audit-Log in PostgreSQL vorhanden (`init-db/001_schema.sql`),
aber keine kryptographische Integritätssicherung.

- [ ] Hash-Chain für Audit-Log-Einträge (SHA-256, jeder Eintrag referenziert Hash des Vorgängers)
- [ ] Append-Only-Constraint auf Audit-Tabelle (kein UPDATE/DELETE via RLS)
- [ ] Integritätsprüfung: MCP-Tool oder CLI-Befehl zum Verifizieren der Hash-Chain
- [ ] Konfigurierbare Log-Retention mit Policy (`data.json`: `audit_retention_days`)
- [ ] Export-Funktion für Audit-Logs (JSON/CSV) für externe Archivierung

### B-41: Transparency Report / Model Card Endpoint (Art. 13 Transparency)
**Priorität:** Hoch
**Aufwand:** ~1.5 Tage
**EU AI Act:** Art. 13 — verständliche Informationen über Systemverhalten für Deployer

- [ ] `GET /transparency` Endpoint auf MCP-Server: maschinenlesbarer Report (JSON)
  - System-Zweck und Einsatzgrenzen
  - Verwendete Modelle (Embedding, Reranker, Summarization) mit Versionen
  - Aktive OPA-Policies und Klassifizierungsstufen
  - PII-Verarbeitungsstatus und Pseudonymisierungs-Methode
  - Datenquellen und letzte Aktualisierung
- [ ] MCP-Tool `get_system_info` für Agents
- [ ] Versionierung des Reports (bei Config-Änderungen neuer Snapshot)

### B-42: Human Oversight Controls (Art. 14 Human Oversight)
**Priorität:** Hoch
**Aufwand:** ~2–3 Tage
**EU AI Act:** Art. 14 — menschliche Aufsicht zur Risikominimierung

Powerbrain hat aktuell keinen Mechanismus für menschliche Intervention.

- [ ] Approval-Queue: OPA-Policy kann Ergebnisse in `pending_review` setzen statt direkt auszuliefern
  - Neue Klassifizierung `requires_approval` in `data.json`
  - Deployer entscheidet per Policy, welche Daten/Aktionen Review brauchen
- [ ] MCP-Tool `review_pending`: Anzeige + Approve/Reject von wartenden Ergebnissen
- [ ] Kill-Switch: `POST /circuit-breaker` — deaktiviert alle Datenauslieferung sofort
  - Persistenter State (überlebt Restart)
  - Nur admin-Rolle
  - Audit-Log-Eintrag bei Aktivierung/Deaktivierung
- [ ] Rate-basierter Auto-Alert: bei ungewöhnlich hohem Zugriff auf `confidential`/`restricted` Daten

### B-43: Data Quality Validation bei Ingestion (Art. 10 Data Governance)
**Priorität:** Mittel
**Aufwand:** ~1.5 Tage
**EU AI Act:** Art. 10 — Daten müssen relevant, repräsentativ, fehlerfrei und vollständig sein

- [ ] Schema-Validierung: Pflichtfelder pro `source_type` prüfen (JSON Schema)
- [ ] Duplikaterkennung: Embedding-Similarity-Check gegen bestehende Dokumente (Threshold konfigurierbar)
- [ ] Qualitäts-Score pro Dokument (Länge, Sprache erkannt, PII-Anteil, Encoding-Fehler)
- [ ] Ingestion-Report: Zusammenfassung pro Batch (accepted/rejected/warnings)
- [ ] OPA-Policy `pb.ingestion.quality_gate`: Mindest-Score konfigurierbar

### B-44: Risk Management Documentation (Art. 9 Risk Management)
**Priorität:** Mittel
**Aufwand:** ~1 Tag
**EU AI Act:** Art. 9 — dokumentiertes, fortlaufendes Risikomanagement über den gesamten Lebenszyklus

- [ ] `docs/risk-management.md` — Template für Deployer:
  - Identifizierte Risiken der Context-Pipeline (Halluzination durch falschen Kontext, PII-Leaks, Policy-Bypass)
  - Mitigationsmaßnahmen (OPA-Policies, PII-Vault, Reranking-Quality)
  - Residual-Risiken und empfohlene Deployer-Maßnahmen
- [ ] Automatisierter Risk-Indikator auf `/health` Endpoint:
  - OPA-Policy-Alter (stale policies = risk)
  - PII-Scanner-Status (disabled = risk)
  - Reranker-Verfügbarkeit (down = quality risk)
  - Audit-Log-Integrität (Hash-Chain-Status)

### B-45: Accuracy Monitoring und Drift Detection (Art. 15 Accuracy/Robustness)
**Priorität:** Mittel
**Aufwand:** ~2 Tage
**EU AI Act:** Art. 15 — Genauigkeit, Robustheit und Cybersicherheit über den gesamten Lebenszyklus

Aktueller Stand: `submit_feedback` + `get_eval_stats` vorhanden, aber kein systematisches Monitoring.

- [ ] Automatische Qualitätsmetriken pro Zeitfenster (gleitend):
  - Durchschnittlicher Feedback-Score
  - Anteil Suchen ohne relevante Ergebnisse (empty results / low reranker scores)
  - Reranker-Score-Verteilung (Drift-Indikator)
- [ ] Alerting bei Qualitätsdrift: Prometheus Alert wenn avg_score unter Threshold fällt
- [ ] Embedding-Drift-Check: Periodischer Vergleich neuer Embeddings gegen Referenz-Set
- [ ] Dashboard-Panel in Grafana: Retrieval Quality über Zeit

### B-46: Technical Documentation Generator (Art. 11 Annex IV)
**Priorität:** Niedrig
**Aufwand:** ~1.5 Tage
**EU AI Act:** Art. 11 + Annex IV — detaillierte technische Dokumentation

- [ ] CLI-Befehl / MCP-Tool `generate_compliance_doc`:
  - Sammelt automatisch: aktive OPA-Policies, Modell-Versionen, Collection-Stats, PII-Config
  - Generiert Annex-IV-konformes Template (Markdown/PDF)
  - Abschnitte: Systemzweck, Datenquellen, Trainings-/Embedding-Modelle, Risiko-Assessment, Monitoring-Metriken
- [ ] Versionierter Output in `docs/compliance/` mit Datum
- [ ] Diff-Ansicht bei Änderungen (was hat sich seit letzter Version geändert)

---

## Backlog — Technische Schulden

### B-20: PipelineStep-Mock in proxy.py aufräumen
**Priorität:** Niedrig
**Aufwand:** ~0.5 Tag

Der `except ImportError`-Fallback definiert einen eigenen `PipelineStep`, der vom Original in `shared/telemetry.py` divergieren kann.

### B-21: ~~Forgejo Workflows → internes Infra-Repo~~
**Erledigt** — `.forgejo/` bleibt im Repo (Coexistence Model). GitHub ignoriert das Verzeichnis.

### B-22: ~~GitHub Actions CI (Pre-Public)~~
**Erledigt** — `.github/workflows/pr-validate.yml` mit 3 Jobs (unit-tests, opa-tests, docker-build).

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
