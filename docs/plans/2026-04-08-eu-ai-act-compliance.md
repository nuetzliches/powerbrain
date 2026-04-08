# EU AI Act Compliance — Implementation Plan

**Datum:** 2026-04-08
**Backlog:** B-40 bis B-46
**EU AI Act Deadline:** 2. August 2026
**Geschätzter Aufwand:** ~12 Tage (parallelisierbar auf ~8 Tage)

## Context

Powerbrain ist als Context Engine kein High-Risk AI System per se, aber Deployer in regulierten Branchen (Finanz, Gesundheit, HR) brauchen von ihrer Infrastruktur die Fähigkeiten aus Art. 9–15. Dieses Feature-Set macht Powerbrain zum "compliance-ready building block" für High-Risk-Systeme.

## Phasen (Dependency-basiert)

```
Phase 1 (parallel):  B-40 Audit Hash-Chain  |  B-44 Risk Management Docs
Phase 2 (parallel):  B-41 Transparency      |  B-43 Data Quality
Phase 3:             B-42 Human Oversight
Phase 4:             B-45 Accuracy Monitoring
Phase 5:             B-46 Compliance Doc Generator
```

B-41 liest Audit-Integritätsstatus aus B-40. B-42 braucht den Transparency-Endpoint für Kill-Switch-Reporting. B-45 erweitert Ingestion-Qualitätsmetriken aus B-43. B-46 sammelt Outputs aller vorherigen Items.

---

## B-40: Tamper-Resistant Audit Logs (Art. 12) — HOCH, ~2 Tage

**EU AI Act Art. 12:** Automatische, manipulationssichere Protokollierung.

**Design:** Hash-Chain via PostgreSQL BEFORE INSERT Trigger (`pgcrypto`). Jeder Audit-Eintrag speichert `SHA-256(prev_hash || id || agent_id || action || resource_id || created_at)`. Application-Code (`log_access()`) bleibt unverändert — Trigger ist transparent.

**Bestehende Architektur (beibehalten):**
- `agent_access_log` Tabelle (`init-db/001_schema.sql`)
- RLS: `mcp_app` = INSERT-only, `mcp_auditor` = SELECT-only (`init-db/008_audit_rls.sql`)
- `log_access()` in `mcp-server/server.py:609` mit PII-Scanning

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/013_audit_hashchain.sql` | NEU | `prev_hash`/`entry_hash` Spalten, BEFORE INSERT Trigger, `pb_verify_audit_chain()` Funktion, `pb_audit_retention_cleanup()` Funktion |
| `opa-policies/pb/data.json` | ÄNDERN | `"audit_retention_days": 365` in `config` |
| `opa-policies/pb/policy_data_schema.json` | ÄNDERN | Schema für `audit_retention_days` |
| `mcp-server/server.py` | ÄNDERN | 2 MCP-Tools: `verify_audit_integrity` (admin), `export_audit_log` (admin) |
| `mcp-server/tests/test_audit_integrity.py` | NEU | Unit Tests |

**Reuse:** `log_access()` bleibt unverändert. Trigger nutzt `pgcrypto` (bereits in PostgreSQL verfügbar).

---

## B-41: Transparency Report Endpoint (Art. 13) — HOCH, ~1.5 Tage

**EU AI Act Art. 13:** Verständliche Informationen über Systemverhalten für Deployer.

**Design:** `GET /transparency` als öffentliche Starlette-Route (neben `/health`). Report gecacht (60s TTL). `report_version` = SHA-256 der statischen Config, ändert sich automatisch bei Config-Änderungen.

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
| `mcp-server/server.py` | ÄNDERN | `transparency_report()` Handler, Route bei ~Zeile 2147, `AUTH_BYPASS_PATHS` erweitern. MCP-Tool `get_system_info` |
| `mcp-server/tests/test_transparency.py` | NEU | Unit Tests |

**Reuse:** OPA-Abfrage via bestehendem `check_opa_policy()`. Qdrant-Client bereits initialisiert. `/health` Pattern als Vorlage.

---

## B-42: Human Oversight Controls (Art. 14) — HOCH, ~2.5 Tage

**EU AI Act Art. 14:** Menschliche Aufsicht zur Risikominimierung.

**Design:**
1. **Circuit Breaker:** State in PostgreSQL `pb_circuit_breaker_state` (überlebt Restart), In-Memory-Cache (5s TTL). `POST /circuit-breaker` (admin-auth). Wenn aktiv: alle Daten-Tools (`search_knowledge`, `query_data`, `get_code_context`, `get_document`) returnen Fehler am Anfang von `_dispatch()`.
2. **Approval Queue:** `pending_reviews` Tabelle. OPA-Policy `pb.oversight.requires_approval` bestimmt, welche Klassifizierungen Review brauchen.
3. **Anomalie-Alert:** Prometheus Counter `pb_confidential_access_total`, Alert bei ungewöhnlich hohem Zugriff.

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/014_human_oversight.sql` | NEU | `pending_reviews`, `pb_circuit_breaker_state` Tabellen |
| `opa-policies/pb/oversight.rego` | NEU | `requires_approval` Regel |
| `opa-policies/pb/oversight_test.rego` | NEU | OPA Tests |
| `opa-policies/pb/data.json` | ÄNDERN | `"human_oversight"` Config-Sektion |
| `opa-policies/pb/policy_data_schema.json` | ÄNDERN | Schema für neue Sektion |
| `mcp-server/server.py` | ÄNDERN | `review_pending` Tool, Circuit-Breaker Endpoints, Approval-Interception in Search-Path |
| `monitoring/alerting_rules.yml` | ÄNDERN | `HighConfidentialAccessRate` Alert |
| `mcp-server/tests/test_human_oversight.py` | NEU | Unit Tests |

**Reuse:** TokenBucket-Pattern als Vorlage für Rate-basiertes Alerting. RLS-Pattern aus `008_audit_rls.sql` für Tabellen-Sicherheit.

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

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/015_data_quality.sql` | NEU | `quality_score`/`quality_details` Spalten auf `documents_meta` |
| `ingestion/quality.py` | NEU | `compute_quality_score()`, `check_duplicate()`, Schema-Validierung |
| `ingestion/quality_schemas/` | NEU | JSON Schemas pro source_type |
| `ingestion/ingestion_api.py` | ÄNDERN | Quality-Pipeline zwischen PII-Scan und Embedding in `ingest_text_chunks()` (~Zeile 478) |
| `opa-policies/pb/ingestion.rego` | NEU | `quality_gate` Regel |
| `opa-policies/pb/ingestion_test.rego` | NEU | OPA Tests |
| `opa-policies/pb/data.json` | ÄNDERN | `"ingestion"` Config-Sektion (min_quality_score, etc.) |
| `ingestion/tests/test_quality.py` | NEU | Unit Tests |

**Reuse:** `EmbeddingProvider` aus `shared/llm_provider.py` für Duplikat-Check. PII-Scanner-Results bereits in Pipeline verfügbar.

---

## B-44: Risk Management Documentation (Art. 9) — MITTEL, ~1 Tag

**EU AI Act Art. 9:** Dokumentiertes, fortlaufendes Risikomanagement.

**Design:** Enhanced `/health` gibt strukturiertes JSON mit Risk-Indikatoren zurück wenn `Accept: application/json` Header gesetzt. Plain-Text "ok" bleibt für Docker/LB Health-Checks.

**Risk-Indikatoren:**
- OPA erreichbar (critical wenn down)
- PII-Scanner Status (high wenn disabled)
- Reranker verfügbar (medium wenn down)
- Audit-Chain Integrität (critical wenn gebrochen)
- Circuit Breaker State (info)
- Feedback-Score (warning wenn <2.5)

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `docs/risk-management.md` | NEU | Art. 9 Template: 6 identifizierte Risiken, Mitigationen, Deployer-Verantwortlichkeiten |
| `mcp-server/server.py` | ÄNDERN | `health_check()` erweitern (~Zeile 2012) |
| `mcp-server/tests/test_health_risk.py` | NEU | Unit Tests |

**Reuse:** Health-Check-Pattern bereits vorhanden. Qdrant/OPA/Reranker Connectivity-Checks existieren in verschiedenen Funktionen.

---

## B-45: Accuracy Monitoring und Drift Detection (Art. 15) — MITTEL, ~2 Tage

**EU AI Act Art. 15:** Genauigkeit, Robustheit und Cybersicherheit über den gesamten Lebenszyklus.

**Design:** Windowed Metrics (1h, 24h, 7d) via SQL View auf `search_feedback`. Background asyncio-Task im MCP-Server aktualisiert Prometheus Gauges alle 5 Minuten. Embedding-Drift-Check vergleicht neue Vektoren gegen Referenz-Set.

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `init-db/016_accuracy_monitoring.sql` | NEU | Windowed Metrics View, `embedding_reference_set` Tabelle |
| `mcp-server/server.py` | ÄNDERN | Neue Prometheus Gauges, Background-Task in Lifespan, `get_eval_stats` erweitern |
| `monitoring/alerting_rules.yml` | ÄNDERN | `QualityDrift`, `HighEmptyResultRate`, `RerankerScoreDrift` Alerts |
| `monitoring/grafana-dashboards/pb-accuracy.json` | NEU | Accuracy Dashboard |
| `shared/drift_check.py` | NEU | Embedding-Drift-Vergleichsfunktion |
| `mcp-server/tests/test_accuracy_monitoring.py` | NEU | Unit Tests |

**Reuse:** `get_eval_stats()` bereits vorhanden (server.py:1714). `pb_feedback_avg_rating` Gauge als Pattern. `MetricsAggregator` aus `shared/telemetry.py`.

---

## B-46: Technical Documentation Generator (Art. 11 / Annex IV) — NIEDRIG, ~1.5 Tage

**EU AI Act Art. 11 + Annex IV:** Detaillierte technische Dokumentation.

**Design:** Separates Modul `compliance_doc.py` fragt alle Datenquellen ab (OPA, Qdrant, PostgreSQL, `/transparency`) und rendert Annex-IV-Template als Markdown. Admin-only MCP-Tool.

**Dateien:**

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `mcp-server/compliance_doc.py` | NEU | `generate_annex_iv_doc()` Funktion |
| `mcp-server/server.py` | ÄNDERN | `generate_compliance_doc` Tool (admin-only) |
| `mcp-server/tests/test_compliance_doc.py` | NEU | Unit Tests |

**Reuse:** `/transparency` Endpoint aus B-41 als primäre Datenquelle.

---

## Zusammenfassung

| Item | Aufwand | Phase | Migration | Neue OPA Policy | Neue MCP-Tools | Neue Endpoints |
|------|---------|-------|-----------|-----------------|----------------|----------------|
| B-40 | 2d | 1 | 013 | — | 2 | — |
| B-41 | 1.5d | 2 | — | — | 1 | `GET /transparency` |
| B-42 | 2.5d | 3 | 014 | `pb.oversight` | 1 | `POST/GET /circuit-breaker` |
| B-43 | 1.5d | 2 | 015 | `pb.ingestion` | — | — |
| B-44 | 1d | 1 | — | — | — | Enhanced `/health` |
| B-45 | 2d | 4 | 016 | — | — | — |
| B-46 | 1.5d | 5 | — | — | 1 | — |

**Neue Migrations:** 013–016 (4 SQL-Dateien)
**Neue OPA Policies:** 2 (`pb.oversight`, `pb.ingestion`)
**Neue MCP-Tools:** 5 (`verify_audit_integrity`, `export_audit_log`, `get_system_info`, `review_pending`, `generate_compliance_doc`)
**Neue Endpoints:** 3 (`/transparency`, `/circuit-breaker`, Enhanced `/health`)

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

# Integration: Audit Hash-Chain
curl -s localhost:8080/transparency | jq '.audit_integrity'

# Integration: Circuit Breaker
curl -X POST localhost:8080/circuit-breaker \
  -H "Authorization: Bearer pb_admin_key" \
  -d '{"active": true, "reason": "test"}'

# Integration: Health mit Risk-Indikatoren
curl -H "Accept: application/json" localhost:8080/health | jq '.risk_level'
```
