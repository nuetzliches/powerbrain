# Architektur-Dokumentation

## 1. Designprinzipien

- **Separation of Concerns**: Jede Komponente hat genau eine Aufgabe
- **MCP-First**: Einziger Zugangspunkt für Agenten ist der MCP-Server
- **GitOps für Regeln**: Business Rules als Rego-Code in Forgejo, versioniert und reviewbar
- **Embeddings lokal**: Ollama im eigenen Container, keine Daten verlassen die Infrastruktur
- **Graceful Degradation**: Jeder optionale Service (Reranker) kann ausfallen, ohne das System zu blockieren

## 2. Komponenten

### 2.1 Qdrant (Vektordatenbank)

Drei Collections, jeweils 768 Dimensionen (nomic-embed-text):

| Collection | Inhalt | Payload-Felder |
|---|---|---|
| `pb_general` | Allgemeines Wissen, Docs | source, type, classification, project, updated_at |
| `pb_code` | Code-Snippets, API-Docs | repo, language, path, classification |
| `pb_rules` | Eingebettete Regelwerke | rule_id, category, severity, active |

### 2.2 PostgreSQL 16

Kernschema (001_schema.sql): datasets, dataset_rows, documents_meta, classifications, agent_access_log, projects.

Datenschutz-Erweiterung (002_privacy.sql): data_categories, data_subjects, deletion_requests, pii_scan_log, v_expiring_data.

JSONB + GIN-Index für flexible Schemas. Alle Metadaten und strukturierten Daten landen hier.

### 2.3 OPA (Open Policy Agent)

Drei Policy-Pakete:

- `pb.access` — Zugriffskontrolle nach Klassifizierung und Rolle
- `pb.rules` — Business Rules (Pricing, Workflow, Compliance)
- `pb.privacy` — DSGVO: Zweckbindung, PII-Behandlung, Aufbewahrungsfristen, Löschrecht

Policies werden als Bundle aus dem Forgejo-Repo `pb-policies` gepollt.

### 2.4 Knowledge Graph (Apache AGE)

Apache AGE ist eine PostgreSQL-Extension, die openCypher-Queries direkt in der bestehenden PostgreSQL-Instanz ermöglicht. Kein separater Graph-Server nötig.

Knotentypen: Project, Technology, Person, Document, Rule, DataSource.

Beziehungstypen: USES, OWNS, DEPENDS_ON, DOCUMENTS, GOVERNS, SOURCED_FROM.

Der Graph ergänzt die Vektorsuche um strukturierte Beziehungen. Ein Agent kann z.B. fragen: "Welche Technologien nutzt Projekt X?" oder "Wer ist verantwortlich für alle Dokumente, die von Regel Y betroffen sind?" — Fragen, die mit reiner Similarity-Search nicht beantwortbar sind.

MCP-Tools: `graph_query` (lesen, traversieren, Pfade finden) und `graph_mutate` (Knoten/Beziehungen erstellen, nur developer/admin).

### 2.5 Reranker (Cross-Encoder)

Eigenständiger FastAPI-Service. Bewertet Query-Dokument-Paare mit einem Cross-Encoder-Modell. Deutlich genauer als Cosine-Similarity, aber langsamer — daher als zweite Stufe nach Qdrant-Oversampling.

Modell-Optionen:
- `cross-encoder/ms-marco-MiniLM-L-6-v2` (Standard, schnell)
- `BAAI/bge-reranker-v2-m3` (multilingual, für DE+EN)

### 2.6 Ollama

Hostet das Embedding-Modell `nomic-embed-text` (768d) lokal. GPU-Support optional. Für höhere Genauigkeit: `mxbai-embed-large` (1024d) — erfordert Anpassung der Qdrant-Collection-Dimension.

### 2.7 Forgejo (extern)

Vorhandene Instanz im Netzwerk. Repositories:
- `pb-policies` — OPA Rego-Dateien
- `pb-schemas` — JSON-Schemas für Datensätze
- `pb-docs` — Technische Dokumentation
- `pb-ingestion-config` — ETL-Templates

## 3. Suchpipeline

```
Query → Embedding (Ollama) → Qdrant (top_k × 5)
  → OPA Policy-Filter → Cross-Encoder Reranking → Top-K Ergebnisse
```

Oversampling-Faktor: 5. Bei top_k=10 holt Qdrant 50 Ergebnisse, OPA filtert z.B. auf 35, der Reranker wählt die 10 relevantesten.

## 4. Datenschutz

### 4.1 PII bei Ingestion

Presidio scannt eingehende Daten. OPA-Policy entscheidet:
- `public` → PII maskieren (`Max Mustermann` → `<PERSON>`)
- `internal` → PII pseudonymisieren (deterministisch, reversibel mit Salt)
- `confidential` → verschlüsselt speichern + Rechtsgrundlage dokumentieren
- `restricted` → PII-Daten werden nicht aufgenommen

### 4.2 Zweckbindung

Jeder MCP-Request enthält `purpose`. OPA prüft gegen erlaubte Verarbeitungszwecke pro Datenkategorie. Reporting-Agenten sehen keine PII-Felder (Datenminimierung via `fields_to_redact`).

### 4.3 Retention

Cronjob (`retention_cleanup.py`) prüft `retention_expires_at` und löscht koordiniert aus PostgreSQL + Qdrant. Audit-Logs werden anonymisiert (nicht gelöscht — Nachweispflicht).

### 4.4 Löschanfragen (Art. 17)

Tabelle `deletion_requests` + `data_subjects`. Der Cleanup-Service prüft gesetzliche Aufbewahrungspflichten bevor er löscht. Status-Tracking: pending → processing → completed/blocked.

### 4.5 Datenschutzvorfälle — LLM-Erkennung und Meldepflicht (Art. 33/34)

**Frage:** Ist es sinnvoll, durch LLMs entdeckte DSGVO-Verstöße zu protokollieren
(„unter den Teppich kehren" vs. dokumentieren)?

#### Rechtliche Einschätzung

**Dokumentieren ist Pflicht — Verbergen dramatisch teurer.**

Die DSGVO-Rechtslage ist eindeutig:

| Norm | Anforderung |
|------|-------------|
| Art. 5(2) | Rechenschaftspflicht: Verantwortlicher muss Compliance **nachweisen können** |
| Art. 33 | Meldung an Aufsichtsbehörde **binnen 72 Stunden** nach Bekanntwerden |
| Art. 34 | Information betroffener Personen bei hohem Risiko |
| Art. 83(4) | Bußgeld bis 10 Mio. € / 2% Jahresumsatz für Art.-33-Verstöße |
| §42 BDSG | Strafbarkeit (bis 3 Jahre Freiheitsstrafe) bei vorsätzlicher Nicht-Meldung |

Der Zeitpunkt des „Bekanntwerdens" (Art. 33 Abs. 1) beginnt, sobald das System
den Vorfall erkennt — nicht erst wenn ein Mensch ihn bewertet. LLM-Erkennung
= Bekanntwerden.

**Praxis der Aufsichtsbehörden (BfDI, LfDI):** Verdeckte Vorfälle werden bei
späteren Audits, Datenschutzfolgenabschätzungen oder Beschwerden aufgedeckt.
Die Behörden verhängen dann 2–4× höhere Bußgelder als bei proaktiver Meldung.
Documented good faith ist der stärkste Bußgeldminderungsgrund (Art. 83 Abs. 2 lit. b, c).

**Das Nicht-Protokollieren** schafft keinerlei Schutz, sondern:
- Verhindert, dass die 72h-Frist rechtzeitig ausgelöst wird (selbst wenn man
  später meldet, ist man schon im Verzug)
- Zerstört den Nachweis, dass das System nach Stand der Technik betrieben wird
- Macht den DSB und ggf. Geschäftsführung persönlich haftbar (§42 BDSG)

#### Was ein LLM erkennen kann

Presidio (Ingestion-Scanner) hat false negatives — besonders bei:
- Implizit identifizierenden Kombinationen (Name + Arbeitgeber + Wohnort, keine
  der drei Angaben ist allein PII)
- Kontextabhängigen Angaben (Pseudonym, das intern einer Person zugeordnet ist)
- Nicht-Standard-Formaten (z.B. Kundennummern die strukturell einem Namen ähneln)

Wenn ein LLM beim Verarbeiten eines Dokuments PII erkennt, die dort nicht sein
sollte, bedeutet das: **die PII wurde bereits in Qdrant eingebettet, möglicherweise
in Audit-Logs geloggt und an andere Agenten zurückgegeben.** Das ist eine
Datenpanne nach Art. 4 Nr. 12 DSGVO.

#### Implementierung: `privacy_incidents`-Tabelle (`006_privacy_incidents.sql`)

Status-Workflow:
```
detected → under_review → contained → notified_authority (wenn meldepflichtig)
                                    → resolved (kein Meldeerfordernis)
         → false_positive (Fehlalarm nach Prüfung)
```

Wichtige Eigenschaften:
- Status-History wird automatisch per Trigger geschrieben (Append-only-Nachweis)
- View `v_incidents_requiring_attention` warnt ab 24h/48h vor Fristablauf
- Index auf `notifiable_risk = true AND authority_notified_at IS NULL` für
  schnellen Zugriff auf offene Meldepflichten
- Tabelle darf niemals geleert werden (gesetzliche Nachweispflicht)

#### Empfohlener Ablauf bei LLM-Erkennung

1. **Automatisch (MCP-Tool oder Agent):** `INSERT INTO privacy_incidents`
   mit `source = 'llm_detection'`, `status = 'detected'`
2. **Sofort:** Betroffene Datasets/Dokumente auf `classification = 'restricted'`
   setzen → OPA sperrt den Zugriff
3. **Binnen 24h:** Menschliche Prüfung: Falsch-Positiv? → `false_positive`.
   Tatsächliche PII? → `contained` + Bewertung `notifiable_risk`
4. **Binnen 72h:** Falls `notifiable_risk = true` → Meldung an BfDI/LfDI,
   `authority_notified_at` setzen
5. **Datenlöschung:** PII aus Qdrant entfernen (Vektoren löschen), PG-Felder
   pseudonymisieren, `resolved` setzen

#### Terminologie

Das System verwendet intern **`quarantined`** (Zugriff gesperrt, wird geprüft)
statt „geleaked" — „leaked" impliziert externen Abfluss, was nicht immer zutrifft
und bei internen PII-Erkennungen unnötige Panik erzeugt. „Geleaked" ist der
Worst-Case-Befund nach der Prüfung, nicht der Ausgangszustand.

## 5. Skalierung

- MCP-Server + Ingestion: Stateless, horizontal skalierbar (Docker Replicas)
- Reranker: Stateless, CPU-intensiv — eigene Skalierung unabhängig vom MCP-Server
- Qdrant: Natives Clustering mit Sharding + Replikation
- PostgreSQL: PgBouncer für Connection Pooling, Citus für horizontales Sharding
- OPA: In-Memory Policy-Evaluation, Bundle-Caching

## 6. Evaluation + Feedback-Loop

Ziel: messbare Retrieval-Qualität. Agenten bewerten Ergebnisse → schlechte Queries werden identifiziert.

### 6.1 Datenbankschema (`004_evaluation.sql`)

- **`search_feedback`** — Rating 1–5 pro Query, inkl. welche Result-IDs hilfreich/irrelevant waren, Rerank-Scores zum Zeitpunkt des Feedbacks
- **`eval_test_set`** — Ground-Truth-Queries mit `expected_ids` und `expected_keywords` für Offline-Evaluation
- **`eval_runs`** — Gespeicherte Evaluierungsläufe mit Precision, Recall, MRR, Latenz und Per-Query-Details

### 6.2 MCP-Tools

- **`submit_feedback`** — Agent bewertet eine Suche (rating 1–5, optional relevant_ids / irrelevant_ids / comment). Rückgabe: `{ feedback_id, stored: true }`
- **`get_eval_stats`** — Statistik für einen Zeitraum (default 30 Tage): avg_rating, satisfaction_pct, Top-10 schlechteste Queries, Trend vs. Vorperiode

### 6.3 Feedback-Loop

Im MCP-Server `search_knowledge`: Bei jeder Suchanfrage wird geprüft, ob die Query in `search_feedback` einen Durchschnitt < 2.5 bei ≥ 3 Feedbacks hat. Falls ja, wird ein Warning geloggt. Langfristig: `OVERSAMPLE_FACTOR` für betroffene Queries erhöhen.

### 6.4 Offline-Evaluator (`evaluation/run_eval.py`)

Eigenständiges Script (Cronjob, z.B. wöchentlich):
1. Liest `eval_test_set` aus PostgreSQL
2. Führt jede Query direkt gegen Qdrant + Reranker aus
3. Berechnet pro Query: Precision@K, Recall@K, MRR, Keyword-Coverage, Latenz
4. Aggregiert und speichert in `eval_runs`
5. Vergleicht mit dem letzten Run → Regression-Alert bei >10% Verschlechterung

```bash
python evaluation/run_eval.py                    # vollständige Evaluierung
python evaluation/run_eval.py --dry-run          # nur ausgeben, nicht speichern
python evaluation/run_eval.py --collection code  # nur eine Collection
```

---

## 7. Wissens-Versionierung

Ziel: Wissensstand zu jedem Zeitpunkt rekonstruierbar. Wichtig für Compliance-Nachweise, Debugging und Rollback nach fehlerhafter Ingestion.

### 7.1 Datenbankschema (`005_versioning.sql`)

- **`knowledge_snapshots`** — Snapshot-Metadaten: Name, Zeitstempel, Ersteller, `components`-JSONB (Qdrant-Snapshot-IDs, PG-Row-Counts, OPA-Policy-Commit-Hash)
- **`datasets_history`** — SCD Type 2: jede Änderung an `datasets` wird automatisch per Trigger mit `valid_from`/`valid_to` aufgezeichnet

### 7.2 Snapshot-Service (`ingestion/snapshot_service.py`)

Funktionen:
- `create_snapshot(name, description)` — Erstellt Qdrant-Snapshots für alle Collections via nativer API (`POST /collections/{name}/snapshots`), speichert PG-Row-Counts und aktuellen Forgejo Policy-Commit-Hash in `knowledge_snapshots`
- `list_snapshots(limit)` — Alle Snapshots mit Metadaten aus PostgreSQL
- `cleanup_old_snapshots(keep_last_n=10)` — Löscht Qdrant-Snapshots + PG-Einträge jenseits des Keep-Limits

Als CLI nutzbar:
```bash
python ingestion/snapshot_service.py --auto          # täglicher Snapshot + Cleanup
python ingestion/snapshot_service.py --list          # Snapshots auflisten
python ingestion/snapshot_service.py --name my-snap  # benannten Snapshot erstellen
```

### 7.3 MCP-Tools

- **`create_snapshot`** — Nur admin. Delegiert an den Ingestion-Service. Rückgabe: `{ snapshot_id, components, created_at }`
- **`list_snapshots`** — Snapshots aus `knowledge_snapshots` auflisten, paginiert via `limit`

---

## 8. Monitoring + Observability

### 8.1 Infrastruktur

| Service | Port | Aufgabe |
|---|---|---|
| Prometheus | 9090 | Metriken sammeln + Alerting-Rules |
| Grafana | 3001 | Dashboards + Alertmanager-UI |
| Grafana Tempo | 3200 / 4317 | Distributed Tracing (OTLP gRPC) |
| postgres-exporter | 9187 | PostgreSQL-Metriken für Prometheus |

### 8.2 Metriken pro Service

**MCP-Server** (`mcp-server:8080/metrics`, Prometheus HTTP-Server):
- `pb_mcp_requests_total{tool, status}` — Requests pro Tool und Status
- `pb_mcp_request_duration_seconds{tool}` — Latenz-Histogramm pro Tool
- `pb_mcp_policy_decisions_total{result}` — OPA allow/deny Zähler
- `pb_mcp_search_results_count{collection}` — Histogramm der Ergebnisanzahl
- `pb_mcp_rerank_fallback_total` — Fallbacks wenn Reranker nicht erreichbar
- `pb_feedback_avg_rating` — Gauge: aktueller Feedback-Schnitt (letzte 24h)

**Reranker** (`reranker:8082/metrics`):
- `pb_reranker_requests_total{status}`
- `pb_reranker_duration_seconds` — Histogramm
- `pb_reranker_batch_size` — Histogramm der Batch-Größen
- `pb_reranker_model_load_seconds` — Modell-Ladezeit beim Start

### 8.3 Grafana Dashboards

Drei vorkonfigurierte Dashboards in `monitoring/grafana-dashboards/`:
1. **KB Overview** — Requests/min, Latenz p50/p95/p99, Error-Rate, Policy-Entscheidungen
2. **Search Quality** — Reranker-Nutzung, Fallback-Rate, Suchergebnis-Histogramme
3. **Infrastructure** — Service-Health, PG Connections, Tool-Volumen

### 8.4 Alerting (`monitoring/alerting_rules.yml`)

| Alert | Bedingung | Severity |
|---|---|---|
| HighErrorRate | Error-Rate > 5% für 5min | warning |
| HighSearchLatency | search p95 > 2s für 10min | warning |
| RerankerDown | `up{job="reranker"} == 0` für 2min | critical |
| LowSearchQuality | `pb_feedback_avg_rating < 2.5` für 1h | warning |
| QdrantDown / PostgresDown | Targets nicht erreichbar für 2min | critical |
| HighRerankerFallbackRate | Fallback-Rate > 10% für 5min | warning |

### 8.5 OpenTelemetry Tracing

Optional via `OTEL_ENABLED=true` im MCP-Server. Traces werden per OTLP gRPC an Grafana Tempo (`http://tempo:4317`) gesendet. Jeder MCP-Tool-Call erzeugt einen Span, Child-Spans für OPA, Qdrant, Reranker, Embedding.

---

## 9. Roadmap

1. ✅ Reranking (Cross-Encoder)
2. ✅ Knowledge Graph (Apache AGE als PG-Extension)
3. ✅ Evaluation + Feedback-Loop (`evaluation/run_eval.py`, MCP-Tools `submit_feedback`/`get_eval_stats`)
4. ✅ Wissens-Versionierung (`ingestion/snapshot_service.py`, MCP-Tools `create_snapshot`/`list_snapshots`)
5. ✅ Monitoring (Prometheus + Grafana + Tempo, `monitoring/`)

---

### Context Layers (L0/L1/L2)

Jedes Dokument wird bei der Ingestion in drei Kontext-Ebenen gespeichert:

| Layer | Inhalt | Tokens | Zweck |
|-------|--------|--------|-------|
| L0 | Abstract (1 Satz) | ~100 | Schnelle Relevanzprüfung |
| L1 | Markdown-Übersicht | ~500 | Entscheidung ob Volltext nötig |
| L2 | Volltext-Chunks | variabel | Detailinformationen (bisheriges Verhalten) |

**Ablauf:**
1. Ingestion erzeugt L2-Chunks (wie bisher)
2. LLM generiert L0 (Abstract) und L1 (Overview) aus den L2-Chunks
3. Alle drei Layer werden als separate Qdrant-Punkte mit `layer`-Payload gespeichert
4. Agents können per `layer`-Parameter gezielt eine Ebene abfragen

**MCP-Integration:**
- `search_knowledge` und `get_code_context`: optionaler `layer`-Parameter (L0/L1/L2)
- `get_document`: Drill-Down von L0 → L1 → L2 per `doc_id`

**OPA-Zugriffssteuerung** (`pb.layers`):
- Admin: immer L2
- Nicht-Admin + confidential: max. L1
- Nicht-Admin + restricted: max. L0
- Viewer + internal: max. L0

**Konfiguration:**
- `LAYER_GENERATION_ENABLED` (default: `true`) — Feature-Flag
- `LLM_MODEL` (default: `qwen2.5:3b`) — Modell für L0/L1-Generierung
- Backfill-Script: `ingestion/backfill_layers.py` für bestehende Daten
