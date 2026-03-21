# Bekannte Schwachstellen & Technische Schulden

Dokumentiert nach Code-Review. Priorisierung: P0 (Blocker), P1 (Sicherheitskritisch),
P2 (Korrektheit), P3 (Architektur).

---

## P0 — Blocker: System startet nicht / Kernfunktion defekt

### P0-1: MCP-Transport: stdio ≠ Docker-Netzwerk

**Datei:** `mcp-server/server.py`, `mcp-server/Dockerfile`

Der MCP-Server nutzt `stdio_server` (stdin/stdout-Pipes). Port 8080 liefert
ausschließlich Prometheus-Metriken. Kein Agent in einem anderen Docker-Container
kann jemals MCP-Tools aufrufen — das Kernversprechen des Projekts ist in der
aktuellen Docker-Konfiguration nicht erfüllbar.

**Fix:** `mcp[server]`'s SSE- oder Streamable-HTTP-Transport verwenden.
Port 8080 als MCP-HTTP-Endpoint, Prometheus auf separatem Port (z.B. 9091).

```python
# Statt:
from mcp.server.stdio import stdio_server

# Benötigt:
from mcp.server.sse import SseServerTransport
# oder (neuere SDK-Version):
from mcp.server.streamable_http import StreamableHTTPServerTransport
```

---

### P0-2: `ingestion_api.py` fehlt

**Datei:** `ingestion/Dockerfile`

```dockerfile
CMD ["uvicorn", "ingestion_api:app", "--host", "0.0.0.0", "--port", "8081"]
```

Diese Datei existiert nicht. Der Service crasht beim Start mit `ModuleNotFoundError`.
Damit sind alle abhängigen Funktionen tot: `ingest_data`, `create_snapshot`,
Ingestion-Pipeline generell.

**Fix:** `ingestion_api.py` mit FastAPI-App implementieren, mindestens:
`POST /ingest`, `POST /snapshots/create`, `GET /health`.

---

### P0-3: `graph_service.py` fehlt im MCP-Server-Image

**Datei:** `mcp-server/Dockerfile`

```dockerfile
COPY server.py .   # graph_service.py wird nicht kopiert
```

`server.py` importiert `graph_service as graph`. Container startet mit `ImportError`.

**Fix:**
```dockerfile
COPY server.py graph_service.py ./
```

---

### P0-4: OPA-Policies werden nicht geladen

**Datei:** `docker-compose.yml`

Der OPA-Container hat kein Volume für `./opa-policies`. OPA läuft leer.
Da `default allow := false` gilt, werden **alle** Anfragen verweigert.

**Fix:** Volume einbinden und Policies beim Start laden:

```yaml
opa:
  volumes:
    - ./opa-policies:/policies
  command:
    - "run"
    - "--server"
    - "--addr=0.0.0.0:8181"
    - "/policies"
```

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

### P2-1: 50 serielle OPA-Calls pro Suchanfrage

**Datei:** `mcp-server/server.py`, `search_knowledge`-Handler

Oversampling holt 50 Treffer, jeder wird einzeln per HTTP gegen OPA geprüft.
Bei 20–50 ms/Call: 1–2,5 Sekunden reiner OPA-Overhead pro Suchanfrage.

**Fix:** OPA-Batch-Evaluation nutzen (`/v1/data` mit Array-Input) oder
Klassifizierungsfilter direkt als Qdrant-Payload-Filter setzen, sodass
OPA nur noch für tatsächlich zurückgegebene Ergebnisse aufgerufen wird.

---

### P2-2: `run_eval.py` umgeht OPA-Policy-Filter

**Datei:** `evaluation/run_eval.py`

Der Offline-Evaluator spricht direkt gegen die Qdrant REST-API ohne Policy-Filter.
Evaluierungsläufe können `restricted`-Dokumente in `eval_runs.details` (JSONB)
persistieren — ein unkontrollierter Kanal für geschützte Daten.

**Fix:** Evaluierung über den MCP-Server mit einem dedizierten `eval`-Role-Token
routen, oder Qdrant-Ergebnisse vor dem Speichern gegen Klassifizierung filtern.

---

### P2-3: `create_snapshot` Endpoint nicht implementiert

**Datei:** `mcp-server/server.py`, `ingestion/snapshot_service.py`

Das MCP-Tool delegiert an `http://ingestion:8081/snapshots/create`. Dieser
Endpoint existiert nicht. `snapshot_service.py` hat kein FastAPI-App, nur
CLI-Funktionen. Das Tool gibt immer einen Connection-Error zurück.

**Fix:** FastAPI-Endpoint in `ingestion_api.py` implementieren, der
`snapshot_service.create_snapshot()` aufruft.

---

### P2-4: Referenz auf nicht existierende Tabelle `business_rules`

**Datei:** `ingestion/snapshot_service.py`

```python
PG_SNAPSHOT_TABLES = ["datasets", "dataset_rows", "business_rules", "documents_meta"]
```

`business_rules` ist in keiner der SQL-Migrationen definiert. Row-Count-Abfrage
schlägt zur Laufzeit fehl.

**Fix:** Tabelle entweder in einer Migration anlegen oder aus der Liste entfernen.

---

### P2-5: PG Connection Pool lazy-initialized

**Datei:** `mcp-server/server.py`

Der Pool wird beim ersten Request erstellt — keine Pre-Warming-Phase, kein
Verbindungstest beim Start. Erste Anfrage nach einem Neustart ist langsam
und kann bei PG-Nichterreichbarkeit im Hintergrund fehlschlagen.

**Fix:** Pool in einem `lifespan`-Context-Manager initialisieren und
`pool.fetchval("SELECT 1")` als Startup-Healthcheck ausführen.

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

### P3-4: Monitoring-Port-Konflikt (MCP-Server)

Port 8080 ist sowohl als MCP-Endpoint (Architektur-Intent) als auch für
Prometheus-Metriken genutzt. Das `prom_start_http_server(8080)` im Hintergrundthread
belegt den Port bevor ein etwaiger HTTP-MCP-Transport ihn nutzen könnte.
Nach Behebung von P0-1 muss der Metrics-Port auf einen anderen Wert (z.B. 9091) wechseln.

---

## Priorisierung für erste Iteration

| Priorität | Issues | Aufwand |
|-----------|--------|---------|
| Sprint 1 (Blocker) | P0-1, P0-2, P0-3, P0-4 | ~3–5 Tage |
| ~~Sprint 2 (Security)~~ | ~~P1-1, P1-2, P1-3~~ | ~~resolved~~ |
| Sprint 3 (Korrektheit) | P2-1, P2-3, P2-5 | ~2–3 Tage |
| Backlog | P2-2, P2-4, P2-6, P3-x | iterativ |

System ist erst nach Sprint 1 + Sprint 2 für **interne Tests** geeignet.
Für **produktiven Einsatz** zusätzlich Sprint 3 + TLS + Secrets Management.

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
