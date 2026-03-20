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

### P1-1: Keine Authentifizierung — Rollen sind selbst-deklariert

**Datei:** `mcp-server/server.py`

`agent_id` und `agent_role` sind einfache Request-Parameter, die der Aufrufer
selbst setzt. Jeder Aufrufer der `agent_role: "admin"` behauptet, hat Admin-Rechte.
OPA prüft die *Behauptung*, nicht die *Identität*.

**Fix:** Vor der Tool-Ausführung ein Bearer-Token (JWT) validieren.
Claims aus dem Token extrahieren, nicht aus Request-Parametern übernehmen.
Minimallösung: API-Key pro Agent mit Rollen-Mapping in PG.

---

### P1-2: SQL-Injection in `query_data`

**Datei:** `mcp-server/server.py`, Zeile ~441

```python
# conditions-Keys werden unsanitiert interpoliert:
where_clauses.append(f"data->>'{key}' = ${idx}")
```

Ein Angreifer mit `conditions = {"x' OR '1'='1": "foo"}` bricht aus der
WHERE-Bedingung aus.

**Fix:** Erlaubte Feldnamen gegen eine Whitelist (Datensatz-Schema) prüfen,
oder die Key-Namen ebenfalls parametrisiert übergeben (JSONB-Operatoren
unterstützen das via `$1::text`-Casting).

---

### P1-3: Cypher-Injection in `graph_service.py`

**Datei:** `mcp-server/graph_service.py`, `_execute_cypher()` / `find_node()`

```python
# Property-Keys werden nie escaped:
where_parts.append(f"n.{k} = {_escape_cypher_value(v)}")
```

Ein Key wie `"id} RETURN n //"` bricht aus dem WHERE aus.
Zusätzlich ist `_escape_cypher_value()` für AGE nicht ausreichend —
AGE-spezifische Escape-Anforderungen weichen von Neo4j ab.

**Fix:** Property-Keys gegen ein definiertes Schema validieren (Whitelist).
Cypher-Queries nicht per String-Interpolation aufbauen, sondern parametrisierte
AGE-Funktionen oder vorbereitete Statement-Templates nutzen.

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
| Sprint 2 (Security) | P1-1, P1-2, P1-3 | ~3–4 Tage |
| Sprint 3 (Korrektheit) | P2-1, P2-3, P2-5 | ~2–3 Tage |
| Backlog | P2-2, P2-4, P2-6, P3-x | iterativ |

System ist erst nach Sprint 1 + Sprint 2 für **interne Tests** geeignet.
Für **produktiven Einsatz** zusätzlich Sprint 3 + TLS + Secrets Management.

---

## Phase 2 nach dem Search-first MVP

Die erste MVP-Iteration konzentriert sich bewusst nur auf den lauffaehigen Suchpfad.
Folgende Themen bleiben danach priorisierte Phase-2-Arbeit:

- Authentifizierung und vertraute Rollenweitergabe
- SQL- und Cypher-Hardening ausserhalb des MVP-Suchpfads
- vollstaendige Ingestion-API
- Snapshot- und Evaluierungs-Nebenpfade
