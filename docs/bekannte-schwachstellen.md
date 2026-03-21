# Bekannte Schwachstellen & Technische Schulden

Dokumentiert nach Code-Review. Priorisierung: P0 (Blocker), P1 (Sicherheitskritisch),
P2 (Korrektheit), P3 (Architektur).

---

## ~~P0 — Blocker: System startet nicht / Kernfunktion defekt~~ — ALL RESOLVED

### ~~P0-1: MCP-Transport: stdio ≠ Docker-Netzwerk~~ — RESOLVED

**Status:** RESOLVED — MCP-Server verwendet nun `StreamableHTTPSessionManager`
auf Port 8080. Prometheus-Metriken auf separatem Port 9091. Agenten in anderen
Docker-Containern können MCP-Tools über HTTP aufrufen.

---

### ~~P0-2: `ingestion_api.py` fehlt~~ — RESOLVED

**Status:** RESOLVED — `ingestion/ingestion_api.py` implementiert mit Endpoints:
`POST /ingest`, `POST /scan`, `POST /snapshots/create`, `GET /health`.

---

### ~~P0-3: `graph_service.py` fehlt im MCP-Server-Image~~ — RESOLVED

**Status:** RESOLVED — `mcp-server/Dockerfile` kopiert nun beide Dateien:
`COPY server.py graph_service.py ./`

---

### ~~P0-4: OPA-Policies werden nicht geladen~~ — RESOLVED

**Status:** RESOLVED — OPA-Container hat Volume `./opa-policies:/policies:ro`
und lädt Policies beim Start via `/policies` Argument.

---

## P1 — Sicherheitskritisch

### ~~P1-1: Keine Authentifizierung — Rollen sind selbst-deklariert~~ — RESOLVED

**Status:** RESOLVED — API-Key-Authentifizierung implementiert. Jeder Agent
benötigt einen `Authorization: Bearer kb_...` Header. Keys werden als SHA-256-Hash
in der `api_keys`-Tabelle gespeichert und bilden auf eine feste Rolle
(analyst/developer/admin) ab. `agent_id` und `agent_role` sind nicht mehr
Tool-Parameter, sondern werden aus dem verifizierten Token abgeleitet.
`AUTH_REQUIRED` Env-Var steuert ob Authentifizierung erzwungen wird (Default: true).

---

### ~~P1-2: SQL-Injection in `query_data`~~ — RESOLVED

**Status:** RESOLVED — Condition-Keys in `query_data` werden nun durch
`validate_identifier()` gegen eine Regex-Whitelist (`^[a-zA-Z_][a-zA-Z0-9_]*$`)
geprüft, bevor sie in SQL-Strings interpoliert werden. Ungültige Keys
liefern einen Fehler zurück. Der `LIMIT`-Wert wird ebenfalls als Parameter
übergeben statt interpoliert. `validate_identifier` ist in `graph_service.py`
definiert und wird von `server.py` importiert.

---

### ~~P1-3: Cypher-Injection in `graph_service.py`~~ — RESOLVED

**Status:** RESOLVED — Alle Graph-Funktionen (`create_node`, `find_node`,
`delete_node`, `create_relationship`, `find_relationships`, `get_neighbors`,
`find_path`) validieren Labels, Property-Keys und Relationship-Types durch
`_require_identifier()`, das intern `validate_identifier()` aufruft.
Nur ASCII-Identifier (`^[a-zA-Z_][a-zA-Z0-9_]*$`) werden akzeptiert.
Ungültige Eingaben werfen `ValueError`.

---

## P2 — Korrektheit / Zuverlässigkeit

### ~~P2-1: 50 serielle OPA-Calls pro Suchanfrage~~ — RESOLVED

**Status:** RESOLVED — OPA-Policy-Checks werden nun mit `asyncio.gather`
parallel ausgeführt statt seriell. Betrifft `search_knowledge`,
`get_code_context` und `list_datasets`. Latenz sinkt von N × RTT auf ~1 × RTT.
Gemeinsame Helper-Funktion `filter_by_policy` für Qdrant-Hits.

---

### P2-2: `run_eval.py` umgeht OPA-Policy-Filter

**Datei:** `evaluation/run_eval.py`

Der Offline-Evaluator spricht direkt gegen die Qdrant REST-API ohne Policy-Filter.
Evaluierungsläufe können `restricted`-Dokumente in `eval_runs.details` (JSONB)
persistieren — ein unkontrollierter Kanal für geschützte Daten.

**Fix:** Evaluierung über den MCP-Server mit einem dedizierten `eval`-Role-Token
routen, oder Qdrant-Ergebnisse vor dem Speichern gegen Klassifizierung filtern.

---

### ~~P2-3: `create_snapshot` Endpoint nicht implementiert~~ — RESOLVED

**Status:** RESOLVED — `ingestion_api.py` implementiert `POST /snapshots/create`,
das `snapshot_service.create_snapshot()` aufruft. MCP-Tool `create_snapshot`
delegiert korrekt an diesen Endpoint.

---

### ~~P2-4: Referenz auf nicht existierende Tabelle `business_rules`~~ — RESOLVED

**Status:** RESOLVED — `business_rules` wurde aus `PG_SNAPSHOT_TABLES` in
`snapshot_service.py` entfernt. Business Rules werden ausschließlich über
OPA-Policies (`kb.rules`) bereitgestellt, nicht über PostgreSQL.

---

### ~~P2-5: PG Connection Pool lazy-initialized~~ — RESOLVED

**Status:** RESOLVED — PG Connection Pool wird nun in einem `lifespan`
async context manager initialisiert mit `SELECT 1` Startup-Healthcheck.
Pool und HTTP-Client werden beim Shutdown sauber geschlossen.
`get_pg_pool()` wirft `RuntimeError` falls Pool nicht initialisiert.

---

### P2-6: Apache AGE — bekannte Einschränkungen

**Datei:** `mcp-server/graph_service.py`

- `shortestPath` hat in bestimmten AGE-Versionen Bugs bei gerichteten Graphen
- `RETURN DISTINCT m, r` bei variabler Tiefe (`[r*1..n]`) gibt in AGE keine
  saubere Liste zurück; das `json.loads(str(row["result"]))`-Parsing ist fragil
- AGE ist deutlich weniger ausgereift als Neo4j; bei komplexen Traversierungen
  mit großen Graphen (>100k Knoten) ist Performance nicht garantiert

**Fix:** AGE-Version pinnen, Integration-Tests für alle Graph-Queries.
Für `shortestPath` einen expliziten Workaround implementieren und dokumentieren.

---

## P3 — Architekturelle Schwächen

### P3-1: Kein Retry / Circuit Breaker

Kurzzeitige Ausfälle von Ollama (~30s beim Modell-Loading), Qdrant oder OPA
lassen alle gleichzeitigen Requests sofort fehlschlagen statt zu queuen.
Empfehlung: `tenacity` für Retry-Logic mit exponential Backoff; Circuit Breaker
für den Reranker (ist ohnehin als optional konzipiert).

---

### P3-2: Kein Rate Limiting

Ein einziger Agent kann den MCP-Server fluten. Kein Token Bucket, kein
Request Queuing, kein Per-Agent Throttling.

---

### P3-3: Ingestion-Pipeline ist ein Stub

CSV/JSON/git_repo-Ingestion ist im MCP-Tool-Schema beschrieben und
in `ingest_data` verdrahtet, aber die eigentliche ETL-Implementierung fehlt.
Die `ingest_data`-Logik delegiert an einen nicht existierenden Endpunkt.

---

### ~~P3-4: Monitoring-Port-Konflikt (MCP-Server)~~ — RESOLVED

**Status:** RESOLVED — Mit P0-1 miterledigt. MCP-Endpoint auf Port 8080,
Prometheus-Metriken auf Port 9091.

---

## Priorisierung für erste Iteration

| Priorität | Issues | Aufwand |
|-----------|--------|---------|
| ~~Sprint 1 (Blocker)~~ | ~~P0-1, P0-2, P0-3, P0-4~~ | ~~resolved~~ |
| ~~Sprint 2 (Security)~~ | ~~P1-1, P1-2, P1-3~~ | ~~resolved~~ |
| ~~Sprint 3 (Korrektheit)~~ | ~~P2-1, P2-3, P2-5~~ | ~~resolved~~ |
| Backlog | P2-2, P2-6, P3-1, P3-2, P3-3 | iterativ |

System ist nach Sprint 1–3 für **interne Tests** geeignet.
Für **produktiven Einsatz** zusätzlich TLS + Secrets Management.

---

## Phase 2 nach dem Search-first MVP

Die erste MVP-Iteration konzentriert sich bewusst nur auf den lauffaehigen Suchpfad.
Folgende Themen bleiben danach priorisierte Phase-2-Arbeit:

- SQL- und Cypher-Hardening ausserhalb des MVP-Suchpfads
- vollstaendige Ingestion-API
- Snapshot- und Evaluierungs-Nebenpfade

---

## Resolved — Sealed Vault (Dual Storage)

Die folgenden Issues wurden im Rahmen der Sealed-Vault-Implementierung behoben:

### RESOLVED: OPA `kb.privacy.pii_action` nie aufgerufen

**Status:** RESOLVED — Die Ingestion-Pipeline ruft nun `kb.privacy.pii_action` auf,
um bei PII-haltigen Daten die korrekte Aktion (pseudonymize, redact, block) zu bestimmen.
Der dual_storage-Pfad nutzt das Ergebnis zur Steuerung der Vault-Einlagerung.

### RESOLVED: `pseudonymize_text()` nie aufgerufen

**Status:** RESOLVED — `pseudonymize_text()` wird jetzt im Dual-Storage-Pfad der
Ingestion-Pipeline aufgerufen, um PII-Texte deterministisch zu pseudonymisieren
bevor sie in Qdrant gespeichert werden. Originale gehen in den Sealed Vault.

### RESOLVED: `pii_scan_log` nie geschrieben

**Status:** RESOLVED — Die Ingestion-Pipeline schreibt nun bei jedem PII-Scan
einen Eintrag in `pii_scan_log` mit Scan-Ergebnis, gefundenen Entity-Typen
und der gewählten Aktion (pseudonymize/redact/block).

### RESOLVED: `fields_to_redact` nie angewendet

**Status:** RESOLVED — Der MCP-Server wendet `vault_fields_to_redact` aus der
OPA-Policy an, wenn Vault-Originale abgerufen werden. Felder werden nach
Purpose redaktiert, sodass nur zweckgebundene Informationen sichtbar sind.

### RESOLVED: Bug in `pseudonymize_text()` — gleicher Pseudonym für alle Entities desselben Typs

**Status:** RESOLVED — Der Bug, bei dem alle Entities desselben Typs (z.B. mehrere
PERSON-Entities) den gleichen Pseudonym-Wert erhielten, wurde behoben. Die Funktion
verwendet nun den Entity-Text als Teil des HMAC-Inputs, sodass jede Entity einen
eindeutigen, aber deterministischen Pseudonym-Wert erhält.

### RESOLVED: `data_subjects`, `datasets.pseudonymized` nie befüllt

**Status:** RESOLVED — Der Vault-Pfad befüllt nun die relevanten Felder:
`data_subjects` werden bei PII-Ingestion mit den erkannten Subjekt-Referenzen
verknüpft, und der Pseudonymisierungsstatus wird korrekt nachgehalten.

---

## Resolved — Audit-Log PII-Schutz

Die folgenden Lücken im Audit-Log wurden behoben:

### RESOLVED: PII in Audit-Log-Query-Text gespeichert

**Status:** RESOLVED — `log_access()` ruft vor dem Speichern den `/scan`-Endpoint
des Ingestion-Service auf. Query-Texte werden durch maskierte Versionen ersetzt
(`"Max Mustermann"` → `"<PERSON>"`). `contains_pii` wird korrekt gesetzt.
`get_code_context` loggt nun ebenfalls konsistent Query-Text (maskiert).

### RESOLVED: `contains_pii` nie gesetzt — Anonymisierung greift nicht

**Status:** RESOLVED — `log_access()` setzt `contains_pii` basierend auf dem
PII-Scan-Ergebnis. Die bestehende Anonymisierung in `retention_cleanup.py`
greift damit korrekt für datensatz-spezifische Löschungen.

### RESOLVED: Keine Zugriffskontrolle auf Audit-Logs

**Status:** RESOLVED — Migration `008_audit_rls.sql` aktiviert Row-Level Security
(mit FORCE) auf `agent_access_log`. `mcp_app` kann nur INSERT, neue Rolle
`mcp_auditor` kann nur SELECT. Kein MCP-Tool exponiert die Logs.

### RESOLVED: Keine zeitbasierte Audit-Log-Retention

**Status:** RESOLVED — Neue Funktion `anonymize_old_audit_logs()` in
`retention_cleanup.py` anonymisiert `request_context` nach `AUDIT_RETENTION_DAYS`
(Default: 365 Tage, konfigurierbar per Env-Var). Integriert als Phase 4 im
Retention-Cleanup-Flow.

### RESOLVED: Hardcoded Ingestion-URL im MCP-Server

**Status:** RESOLVED — `INGESTION_URL` wird nun aus Umgebungsvariable gelesen
(Default: `http://ingestion:8081`). Alle Ingestion-Aufrufe in `server.py`
verwenden die konfigurierbare URL.
