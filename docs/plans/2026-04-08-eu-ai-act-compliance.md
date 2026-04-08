# EU AI Act Compliance — Implementation Plan

**Datum:** 2026-04-08 (review-updated)
**Backlog:** B-40 bis B-46 + pb-worker
**EU AI Act Deadline:** 2. August 2026
**Geschätzter Aufwand:** ~13 Tage (parallelisierbar auf ~9 Tage)

> **Review-Update (2026-04-08):** Plan wurde gegen den aktuellen Code verifiziert. Migrationsnummern korrigiert (014–017 statt 013–016), pgcrypto-Aktivierung ergänzt, neue Scope-Ergänzung `pb-worker` Maintenance-Container, sowie finalisierte Designentscheidungen für alle Items. Details siehe `docs/plans/2026-04-08-eu-ai-act-compliance-review.md` (Abstimmungsdokument).

## Context

Powerbrain ist als Context Engine kein High-Risk AI System per se, aber Deployer in regulierten Branchen (Finanz, Gesundheit, HR) brauchen von ihrer Infrastruktur die Fähigkeiten aus Art. 9–15. Dieses Feature-Set macht Powerbrain zum "compliance-ready building block" für High-Risk-Systeme.

## Phasen (Dependency-basiert)

```
Phase 1 (parallel):  B-40 Audit Hash-Chain  |  B-44 Risk Management Docs
Phase 2 (parallel):  B-41 Transparency      |  B-43 Data Quality
Phase 3:             B-42 Human Oversight   |  pb-worker Skeleton
Phase 4:             B-45 Accuracy Monitoring (nutzt pb-worker)
Phase 5:             B-46 Compliance Doc Generator
```

B-41 liest Audit-Integritätsstatus aus B-40. B-42 braucht den Transparency-Endpoint für Kill-Switch-Reporting. B-45 erweitert Ingestion-Qualitätsmetriken aus B-43. B-46 sammelt Outputs aller vorherigen Items. `pb-worker` wird vor B-45 als leeres Skelett aufgesetzt, damit B-40-Cleanup, B-42-Timeout und B-45-Metrics-Refresh als Jobs andocken können.

---

## B-40: Tamper-Resistant Audit Logs (Art. 12) — HOCH, ~2 Tage

**EU AI Act Art. 12:** Automatische, manipulationssichere Protokollierung.

**Design:** Hash-Chain via PostgreSQL BEFORE INSERT Trigger. Jeder Audit-Eintrag speichert `SHA-256(prev_hash || id || agent_id || action || resource_id || created_at)`. Application-Code (`log_access()`) bleibt unverändert — Trigger ist transparent.

**Concurrency:** Parallele INSERTs werden via `pg_advisory_xact_lock(<audit_lock_id>)` im Trigger serialisiert (nur Audit-Writes, andere Tx bleiben parallel).

**Retention / DSGVO:** "Checkpoint + Prune" statt direktem Hard-Delete.
1. `pb_verify_audit_chain(start_id, end_id)` verifiziert zu löschenden Bereich
2. Bei Erfolg: Eintrag in neue Tabelle `audit_archive` (archived_at, last_entry_id, last_verified_hash, row_count, chain_valid)
3. Hard-Delete der Rows bis `last_entry_id`
4. Neue Chain setzt mit `prev_hash = audit_archive.last_verified_hash` fort → mathematisch durchgehend trotz gelöschter Zwischenglieder

**Bestehende Architektur (beibehalten):**
- `agent_access_log` Tabelle (`init-db/001_schema.sql`)
- RLS: `mcp_app` = INSERT-only, `mcp_auditor` = SELECT-only (`init-db/008_audit_rls.sql`)
- `log_access()` in `mcp-server/server.py:609` mit PII-Scanning

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/014_audit_hashchain.sql` | NEU | `CREATE EXTENSION IF NOT EXISTS pgcrypto`, `prev_hash`/`entry_hash` Spalten auf `agent_access_log`, Advisory-Lock BEFORE INSERT Trigger, `audit_archive` Tabelle (RLS wie 008), `pb_verify_audit_chain()` und `pb_audit_checkpoint_and_prune()` Funktionen |
| `opa-policies/pb/data.json` | ÄNDERN | Neue Sektion `"audit": {"retention_days": 365, "advisory_lock_id": 847291}` |
| `opa-policies/pb/policy_data_schema.json` | ÄNDERN | Schema für `audit`-Sektion |
| `mcp-server/server.py` | ÄNDERN | 2 MCP-Tools: `verify_audit_integrity` (admin, optional Zeitraum), `export_audit_log` (admin, JSON/CSV, Filter: Zeitraum/agent_id/action, Max-Zeilen) |
| `mcp-server/tests/test_audit_integrity.py` | NEU | Unit Tests (Chain-Verify, Tamper-Detection, Checkpoint-Fortschreibung) |

**Reuse:** `log_access()` bleibt unverändert — Trigger wirkt transparent darunter. `008_audit_rls.sql` als Vorlage für `audit_archive`-RLS. Der eigentliche `audit_retention_cleanup`-Job läuft im neuen `pb-worker` (siehe unten), nicht als DB-Cron.

---

## B-41: Transparency Report Endpoint (Art. 13) — HOCH, ~1.5 Tage

**EU AI Act Art. 13:** Verständliche Informationen über Systemverhalten für Deployer.

**Design:** `GET /transparency` als auth-required Starlette-Route. Jeder gültige `pb_`-API-Key darf abrufen (Art. 13 adressiert Deployer, nicht die Öffentlichkeit — verhindert Infolek). Route wird **nicht** in `AUTH_BYPASS_PATHS` aufgenommen. Report gecacht (60s TTL). `report_version` = SHA-256 über Modell-Env-Vars + OPA-Config-Hash + Collection-Liste; Cache-Refresh beim nächsten Hit nach TTL.

**Report-Inhalt:**
- System-Zweck und Einsatzgrenzen
- Modell-Versionen (Embedding, Reranker, Summarization) aus Env-Vars
- Aktive OPA-Policies aus `GET {OPA_URL}/v1/data/pb/config`
- Qdrant Collection-Stats
- PII-Scanner-Config aus Ingestion `/health`
- Audit-Chain-Integrität aus B-40

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `mcp-server/server.py` | ÄNDERN | `transparency_report()` Handler, Route-Registrierung (~Zeile 2147). Auth bleibt aktiv, d.h. **kein** Eintrag in `AUTH_BYPASS_PATHS`. Zusätzlich MCP-Tool `get_system_info` |
| `mcp-server/tests/test_transparency.py` | NEU | Unit Tests (Auth-Pflicht, Cache-Invalidierung bei Config-Änderung) |

**Reuse:** OPA-Abfrage via bestehendem `check_opa_policy()`. Qdrant-Client bereits initialisiert. `/health` Pattern als Vorlage — aber ohne Auth-Bypass.

---

## B-42: Human Oversight Controls (Art. 14) — HOCH, ~2.5 Tage

**EU AI Act Art. 14:** Menschliche Aufsicht zur Risikominimierung.

**Design:**
1. **Circuit Breaker (Globaler Kill-Switch):** Single-Row-Tabelle `pb_circuit_breaker_state` (überlebt Restart), In-Memory-Cache (5s TTL). `POST /circuit-breaker` (admin-auth) toggelt den Schalter. Wenn aktiv: alle Daten-Tools (`search_knowledge`, `query_data`, `get_code_context`, `get_document`) returnen am Anfang von `_dispatch()` (server.py:1206) sofort einen Fehler mit `reason`. Keine Granularität pro Rolle/Classification — bewusst einfache Art. 14 Semantik.
2. **Approval Queue (Async-Flow):** `pending_reviews` Tabelle. OPA-Policy `pb.oversight.requires_approval` bestimmt pro Request (Rolle × Classification × Action), ob Review nötig ist. Wenn ja, legt der Search-Pfad eine Review-Zeile an und returnt sofort `{status: "pending", review_id: <uuid>}`. Agent pollt mit neuem Tool `get_review_status`. Admin entscheidet via Tool `review_pending` (approve/deny). Bei Approval wird der ursprüngliche Query im Review-Datensatz gespeichert und beim nächsten `get_review_status`-Poll mit Ergebnissen beantwortet.
3. **Timeout/Escalation:** `pending_review_timeout` Konfig. Job im `pb-worker` setzt abgelaufene Reviews auf `expired` und feuert Prometheus-Alert.
4. **Anomalie-Alert:** Prometheus Counter `pb_confidential_access_total`, Alert bei ungewöhnlich hohem Zugriff (TokenBucket-Pattern als Vorlage).

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/015_human_oversight.sql` | NEU | `pending_reviews` (uuid, agent_id, agent_role, tool, arguments JSONB, classification, status, decision_by, decision_at, expires_at), `pb_circuit_breaker_state` (single row, active BOOL, reason TEXT, set_by, set_at), RLS wie 008 |
| `opa-policies/pb/oversight.rego` | NEU | `requires_approval` Regel, Daten aus `data.pb.human_oversight` |
| `opa-policies/pb/oversight_test.rego` | NEU | OPA Tests |
| `opa-policies/pb/data.json` | ÄNDERN | `"human_oversight": {requires_approval_matrix, pending_review_timeout_minutes, max_pending_per_agent}` Sektion |
| `opa-policies/pb/policy_data_schema.json` | ÄNDERN | Schema für neue Sektion |
| `mcp-server/server.py` | ÄNDERN | Circuit-Breaker-Gate zu Beginn von `_dispatch()` (~1206), `POST/GET /circuit-breaker` Route, zwei MCP-Tools `review_pending` (admin) + `get_review_status` (all roles), Approval-Interception im Search-Pfad |
| `monitoring/alerting_rules.yml` | ÄNDERN | `HighConfidentialAccessRate`, `PendingReviewExpired` Alerts |
| `mcp-server/tests/test_human_oversight.py` | NEU | Unit Tests (Kill-Switch blockt Dispatch, Async-Flow End-to-End, Timeout) |

**Reuse:** TokenBucket-Pattern (server.py:201) als Vorlage für Rate-basiertes Alerting. RLS-Pattern aus `008_audit_rls.sql`. `pb-worker` für Timeout-Job.

---

## B-43: Data Quality Validation bei Ingestion (Art. 10) — MITTEL, ~1.5 Tage

**EU AI Act Art. 10:** Daten müssen relevant, repräsentativ, fehlerfrei und vollständig sein.

**Design:** Quality-Score (0.0–1.0) aus 5 gewichteten Faktoren:
- Textlänge (0.25) — zu kurz/lang penalisiert
- Spracherkennung-Confidence (0.20)
- PII-Anteil (0.20) — hoher PII-Anteil = niedrigerer Score
- Encoding-Sauberkeit (0.15) — mojibake, Kontrollzeichen
- Metadata-Vollständigkeit (0.20) — Pflichtfelder pro source_type

Duplikaterkennung via Cosine-Similarity des First-Chunk-Embeddings (Threshold konfigurierbar, Default 0.95).

**Gate-Verhalten:** Blockierend. Dokumente unter `min_quality_score` werden rejected (OPA-Policy `pb.ingestion.quality_gate`). Schwelle pro `source_type` konfigurierbar (z.B. `code` lockerer als `contracts`).

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/016_data_quality.sql` | NEU | `quality_score` (REAL) + `quality_details` (JSONB) Spalten auf `documents_meta`, Index auf quality_score |
| `ingestion/quality.py` | NEU | `compute_quality_score()`, `check_duplicate()`, Schema-Validierung |
| `ingestion/quality_schemas/` | NEU | JSON Schemas pro source_type |
| `ingestion/ingestion_api.py` | ÄNDERN | Quality-Pipeline zwischen PII-Scan und Embedding in `ingest_text_chunks()` (~Zeile 478). Bei Gate-Fail: rejected log + frühes Return mit `{"status": "rejected", "reason": ..., "quality_score": ...}` |
| `opa-policies/pb/ingestion.rego` | NEU | `quality_gate` Regel, liest `min_quality_score` map aus `data.pb.ingestion` |
| `opa-policies/pb/ingestion_test.rego` | NEU | OPA Tests |
| `opa-policies/pb/data.json` | ÄNDERN | `"ingestion": {"min_quality_score": {"default": 0.6, "code": 0.4, "contracts": 0.8}, "duplicate_threshold": 0.95}` Sektion |
| `opa-policies/pb/policy_data_schema.json` | ÄNDERN | Schema für `ingestion`-Sektion |
| `ingestion/tests/test_quality.py` | NEU | Unit Tests |

**Reuse:** `EmbeddingProvider.embed_batch()` aus `shared/llm_provider.py` für Duplikat-Check. PII-Scanner-Results bereits in Pipeline verfügbar. `check_opa_privacy()`-Pattern als Vorlage für `check_opa_ingestion_quality()`.

---

## B-44: Risk Management Documentation (Art. 9) — MITTEL, ~1 Tag

**EU AI Act Art. 9:** Dokumentiertes, fortlaufendes Risikomanagement.

**Design:** Enhanced `/health` gibt strukturiertes JSON mit Risk-Indikatoren zurück **nur wenn** `Accept: application/json` Header gesetzt ist. Plain-Text `"ok"` bleibt Default für Docker/LB Health-Checks (Backwards Compat).

**Risk-Indikatoren:**
- OPA erreichbar (critical wenn down)
- PII-Scanner Status (high wenn disabled)
- Reranker verfügbar (medium wenn down)
- Audit-Chain Integrität (critical wenn gebrochen)
- Circuit Breaker State (info)
- Feedback-Score (warning wenn <2.5)

**Risk-Register (`docs/risk-management.md`):** Konkretes Powerbrain-Risk-Register (kein generisches Template). Enthält mindestens: LLM-Halluzination, PII-Leak im Pseudo-Pfad, Embedding-Drift, Audit-Chain-Bruch, OPA-Ausfall, Vault-Kompromittierung, Input-Injection über Suchtexte. Je Risiko: Beschreibung, Likelihood, Impact, Mitigation (implementiert), Residual Risk, Deployer-Verantwortung.

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `docs/risk-management.md` | NEU | Konkretes Powerbrain-Risk-Register (Art. 9) mit ≥7 Risiken und Mitigationen |
| `mcp-server/server.py` | ÄNDERN | `health_check()` erweitern (~Zeile 2012), Content-Negotiation auf `Accept` Header |
| `mcp-server/tests/test_health_risk.py` | NEU | Unit Tests (Plain-Text Default, JSON via Accept, Indikator-Werte) |

**Reuse:** Health-Check-Pattern bereits vorhanden. Qdrant/OPA/Reranker Connectivity-Checks existieren in verschiedenen Funktionen. Audit-Chain-Check via `pb_verify_audit_chain()` aus B-40.

---

## B-45: Accuracy Monitoring und Drift Detection (Art. 15) — MITTEL, ~2 Tage

**EU AI Act Art. 15:** Genauigkeit, Robustheit und Cybersicherheit über den gesamten Lebenszyklus.

**Design:** Windowed Metrics (1h, 24h, 7d) via SQL View `v_feedback_windowed` auf `search_feedback`. Metrics-Refresh läuft **im neuen `pb-worker`-Container** (nicht im mcp-server Prozess) alle 5 Minuten. Embedding-Drift-Check vergleicht neue Vektoren gegen Referenz-Set.

**Reference Set:** Deployment-Snapshot. Beim ersten `pb-worker` Start samplet der Worker N Dokumente pro Collection aus Qdrant und speichert deren Embeddings in `embedding_reference_set` als Baseline. Reproduzierbar via `ingestion/snapshot_service.py`. Re-Sampling nur manuell (Admin-Tool oder explizit neu seeden).

**Drift-Threshold pro Collection** in `data.json` (`drift.thresholds: {pb_general: 0.08, pb_code: 0.12, pb_rules: 0.05}`).

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/017_accuracy_monitoring.sql` | NEU | Windowed Metrics View `v_feedback_windowed`, `embedding_reference_set` Tabelle (collection, doc_id, embedding VECTOR, created_at) |
| `worker/jobs/accuracy_metrics.py` | NEU | Job: Metrics-View abfragen, Prometheus Gauges pushen, `drift_check.compute_drift()` pro Collection aufrufen |
| `mcp-server/server.py` | ÄNDERN | Neue Prometheus Gauges (registriert zur Einsicht via `/metrics/json`), `get_eval_stats` um Windowed-Werte erweitern |
| `monitoring/alerting_rules.yml` | ÄNDERN | `QualityDrift`, `HighEmptyResultRate`, `RerankerScoreDrift` Alerts |
| `monitoring/grafana-dashboards/pb-accuracy.json` | NEU | Accuracy Dashboard |
| `shared/drift_check.py` | NEU | Embedding-Drift-Vergleichsfunktion (Cosine-Centroid-Distance, per-collection Threshold) |
| `worker/tests/test_accuracy_job.py` | NEU | Unit Tests |

**Reuse:** `get_eval_stats()` bereits vorhanden (server.py:1714). `pb_feedback_avg_rating` Gauge als Pattern. `MetricsAggregator` aus `shared/telemetry.py`. `ingestion/snapshot_service.py` für Baseline-Seed.

---

## B-46: Technical Documentation Generator (Art. 11 / Annex IV) — NIEDRIG, ~1.5 Tage

**EU AI Act Art. 11 + Annex IV:** Detaillierte technische Dokumentation.

**Design:** Separates Modul `compliance_doc.py` fragt alle Datenquellen ab (OPA, Qdrant, PostgreSQL, `/transparency`) und rendert Annex-IV-Template als Markdown (**EN only** — Standard für EU AI Act Dokumente). Admin-only MCP-Tool mit Parameter `output_mode: "inline" | "file"` (Default `inline`). Bei `file` wird in konfigurierbaren Pfad geschrieben und der Pfad zurückgegeben.

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `mcp-server/compliance_doc.py` | NEU | `generate_annex_iv_doc(output_mode: Literal["inline","file"])` Funktion, EN-Template |
| `mcp-server/server.py` | ÄNDERN | `generate_compliance_doc` Tool (admin-only), Parameter `output_mode` |
| `mcp-server/tests/test_compliance_doc.py` | NEU | Unit Tests (Inline Default, File-Mode, alle Annex-IV-Sektionen befüllt) |

**Reuse:** `/transparency` Endpoint aus B-41 als primäre Datenquelle.

---

## pb-worker: Maintenance Container (Scope-Ergänzung) — ~1 Tag

**Motivation:** Aktuell gibt es kein Zuhause für periodische Wartungsjobs. `retention_cleanup.py` ist nur ein CLI-Script ohne Scheduler. Mehrere neue Items (B-40 Audit-Cleanup, B-45 Metrics-Refresh, B-42 Review-Timeout) brauchen einen Scheduler. Ein dedizierter Container konsolidiert die Wartung.

**Service:** `pb-worker` — neuer Docker-Service, teilt Image-Basis mit `ingestion` (gleiche DB/Qdrant-Clients + APScheduler). Kein offener Port (interner Metrics-Endpoint optional).

**Scheduler:** `APScheduler` (AsyncIOScheduler) im Python-Prozess.

**Jobs:**

| Job | Interval | Quelle | Beschreibung |
|---|---|---|---|
| `accuracy_metrics_refresh` | alle 5 min | B-45 | View `v_feedback_windowed` lesen, Gauges pushen, `drift_check` pro Collection |
| `audit_retention_cleanup` | täglich 03:00 | B-40 | `pb_verify_audit_chain` → `pb_audit_checkpoint_and_prune` |
| `gdpr_retention_cleanup` | täglich 02:00 | bestehendes `retention_cleanup.py` migriert | Logik aus CLI in Worker-Job verschieben; `--execute` als Default im Container, CLI bleibt für manuelle Dry-Runs |
| `pending_review_timeout` | stündlich | B-42 | Abgelaufene Reviews auf `expired` setzen, Alert feuern |

**Dateien:**

| Datei | Aktion | Beschreibung |
|---|---|---|
| `worker/scheduler.py` | NEU | APScheduler-Setup + Job-Registrierung + Lifespan |
| `worker/jobs/accuracy_metrics.py` | NEU | B-45 Job |
| `worker/jobs/audit_retention.py` | NEU | B-40 Job |
| `worker/jobs/gdpr_retention.py` | NEU | Logik aus `ingestion/retention_cleanup.py` hierher verschieben |
| `worker/jobs/pending_review_timeout.py` | NEU | B-42 Job |
| `worker/Dockerfile` | NEU | Basis `python:3.12-slim`, installiert `worker/requirements.txt` + `shared/` |
| `worker/requirements.txt` | NEU | `apscheduler`, `asyncpg`, `qdrant-client`, `prometheus-client`, `httpx` |
| `worker/tests/test_jobs.py` | NEU | Unit Tests je Job |
| `ingestion/retention_cleanup.py` | ÄNDERN | Bleibt als dünner Wrapper/CLI um den Worker-Job (Import aus `worker/jobs/gdpr_retention.py`) |
| `docker-compose.yml` | ÄNDERN | Neuer Service `pb-worker` mit `depends_on: postgres, qdrant, opa`, keine Ports, `pb-net` Netzwerk |

**Reuse:** `build_postgres_url()` aus `shared/config.py`, `EmbeddingProvider` aus `shared/llm_provider.py`, `init_telemetry()` aus `shared/telemetry.py`.

---

## Zusammenfassung

| Item | Aufwand | Phase | Migration | Neue OPA Policy | Neue MCP-Tools | Neue Endpoints |
|------|---------|-------|-----------|-----------------|----------------|----------------|
| B-40 | 2d | 1 | 014 | — | 2 | — |
| B-41 | 1.5d | 2 | — | — | 1 | `GET /transparency` (auth) |
| B-42 | 2.5d | 3 | 015 | `pb.oversight` | 2 | `POST/GET /circuit-breaker` |
| B-43 | 1.5d | 2 | 016 | `pb.ingestion` | — | — |
| B-44 | 1d | 1 | — | — | — | Enhanced `/health` |
| B-45 | 2d | 4 | 017 | — | — | — |
| B-46 | 1.5d | 5 | — | — | 1 | — |
| pb-worker | 1d | 3 | — | — | — | — |

**Neue Migrations:** 014–017 (4 SQL-Dateien)
**Neue OPA Policies:** 2 (`pb.oversight`, `pb.ingestion`)
**Neue MCP-Tools:** 6 (`verify_audit_integrity`, `export_audit_log`, `get_system_info`, `review_pending`, `get_review_status`, `generate_compliance_doc`)
**Neue Endpoints:** 3 (`/transparency` auth-required, `/circuit-breaker`, Enhanced `/health`)
**Neue Services:** 1 (`pb-worker` Maintenance Container)

## Verifizierung

```bash
# Unit Tests (alle neuen Tests)
PYTHONPATH=.:mcp-server:ingestion:reranker:pb-proxy \
python -m pytest mcp-server/tests/test_audit_integrity.py \
                 mcp-server/tests/test_transparency.py \
                 mcp-server/tests/test_human_oversight.py \
                 mcp-server/tests/test_health_risk.py \
                 mcp-server/tests/test_accuracy_monitoring.py \
                 mcp-server/tests/test_compliance_doc.py \
                 ingestion/tests/test_quality.py \
                 -v

# OPA Tests (inkl. neue Policies)
docker exec pb-opa /opa test /policies/pb/ -v

# Integration: Audit Hash-Chain (auth required)
curl -s -H "Authorization: Bearer pb_admin_key" \
  localhost:8080/transparency | jq '.audit_integrity'

# Integration: Circuit Breaker
curl -X POST localhost:8080/circuit-breaker \
  -H "Authorization: Bearer pb_admin_key" \
  -d '{"active": true, "reason": "test"}'

# Integration: Health mit Risk-Indikatoren
curl -H "Accept: application/json" localhost:8080/health | jq '.risk_level'
```
