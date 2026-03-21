# Sprint 5 Design: P2-6 + P3-2 + P3-3

**Datum:** 2026-03-21
**Scope:** Apache AGE Hardening, Rate Limiting, Ingestion Cleanup + Chunk-API

---

## Übersicht

| Issue | Titel | Art | Aufwand |
|-------|-------|-----|---------|
| P2-6 | Apache AGE Einschränkungen | Bugfix + Hardening | Mittel |
| P3-2 | Kein Rate Limiting | Neues Feature | Mittel |
| P3-3 | Ingestion-Pipeline Stubs | Aufräumen + API | Mittel |

---

## P2-6: Apache AGE Hardening

### Problem

1. **Fehlende `graph_sync_log` Tabelle:** `_log_sync()` in `graph_service.py:333`
   schreibt in eine Tabelle die in keiner Migration existiert. Jede Graph-Mutation
   crasht beim Logging.
2. **Fragiles agtype-Parsing:** `json.loads(str(raw))` bricht bei AGE-spezifischen
   Suffixen (`::vertex`, `::edge`).
3. **`shortestPath` Bugs:** Bekannte Probleme in AGE bei gerichteten Graphen.
4. **Variable-Depth Traversal:** `[r*1..depth]` gibt Pfade/Listen statt einzelner
   Relationships zurück.

### Design

**Migration `011_graph_sync_log.sql`:**
- Tabelle mit: `id SERIAL`, `operation TEXT`, `label TEXT`, `node_id TEXT`,
  `details JSONB`, `created_at TIMESTAMPTZ DEFAULT now()`

**`graph_service.py` Hardening:**
- `_execute_cypher()`: AGE agtype-Suffixe (`::vertex`, `::edge`, `::path`) per
  Regex strippen vor `json.loads`. Robusteres Fallback-Parsing.
- `find_path()`: Try/except um `shortestPath()`. Bei AGE-Fehler Fallback auf
  iterativen BFS via `get_neighbors()` mit Tiefenlimit.
- Variable-Depth: Ergebnis als Liste parsen wenn `*` in der Query.

**Tests:**
- Migration definiert `graph_sync_log` Tabelle
- `_execute_cypher` hat agtype-Suffix-Handling (Regex-Pattern)
- `find_path` hat Fallback-Logik

---

## P3-2: Rate Limiting

### Problem

Kein Rate Limiting — ein Agent kann den MCP-Server fluten.

### Design

**In-Memory Token Bucket pro `agent_id`, Limits pro Rolle via Env-Vars.**

**Konfiguration:**
```
RATE_LIMIT_ANALYST=60       # Requests pro Minute
RATE_LIMIT_DEVELOPER=120
RATE_LIMIT_ADMIN=300
RATE_LIMIT_ENABLED=true     # Komplett deaktivierbar
```

**Token Bucket:**
- `TokenBucket` Klasse mit Kapazität, Refill-Rate, asyncio Lock
- Registry: `dict[str, TokenBucket]` — ein Bucket pro `agent_id`
- Cleanup: Buckets nach 10 Min Inaktivität entfernen

**Integration:**
- Starlette Middleware nach Auth, vor MCP-Dispatch
- HTTP 429 mit `Retry-After` Header bei Überschreitung
- Prometheus Counter `kb_rate_limit_rejected_total`
- `/health` und `/metrics` ausgenommen

**Graceful Degradation:**
- Rate Limiter Fehler → Requests durchlassen (fail-open)

**Tests:**
- Env-Var-Konfiguration wird gelesen
- Middleware in der Chain vorhanden
- 429 Response-Logik existiert
- Fail-open Pattern vorhanden

---

## P3-3: Ingestion Aufräumen + Chunk-API

### Problem

`/ingest` mischt Source-Parsing mit KB-Pipeline. `git_repo` und `sql_dump` sind
Stubs. Adapter (Forgejo, etc.) brauchen einen sauberen Einstiegspunkt.

### Architektur-Entscheidung

`/ingest` ist der Endpoint über den Agenten Text in die KB schreiben.
Adapter (Forgejo, CSV-Imports, etc.) sind separate Komponenten die Daten
vorverarbeiten und über einen internen Chunk-Endpoint einspeisen.
Beide Wege durchlaufen die gleiche Datenschutz-Pipeline (`ingest_text_chunks()`).

```
Agent (MCP ingest_data) → POST /ingest       → ingest_text_chunks()
Adapter (intern)        → POST /ingest/chunks → ingest_text_chunks()
                                                  ↓
                                            PII → OPA → Vault → Embed → Qdrant
```

### Design

**Stubs entfernen:**
- `git_repo` und `sql_dump` Branches aus `/ingest` entfernen
- MCP-Tool `ingest_data` Schema: `source_type` Enum auf `["text"]` reduzieren
- Klare Fehlermeldung bei unbekanntem `source_type`

**Neuer Endpoint `POST /ingest/chunks`:**
```python
class ChunkIngestRequest(BaseModel):
    chunks: list[str]                    # Vorverarbeitete Text-Chunks
    project: str
    collection: str = "knowledge_general"
    classification: str = "internal"
    metadata: dict = {}
    source: str = ""                     # Herkunftsbezeichnung
```
- Ruft `ingest_text_chunks()` auf — volle Pipeline
- Nur aus Docker-Netzwerk erreichbar (kein externer Zugang)

**`/ingest` vereinfachen:**
- Nur `text` als Source-Type
- Chunking + `ingest_text_chunks()`
- Kein Source-Type-Switch

**Tests:**
- MCP-Tool Schema hat nur `text` als Source-Type
- `/ingest/chunks` Endpoint existiert
- ChunkIngestRequest Model hat erwartete Felder

---

## Abhängigkeiten

Die drei Tasks sind voneinander unabhängig und können parallel implementiert werden.
Task 4 (Docker rebuild + live verify) hängt von allen dreien ab.

## Offene Punkte für spätere Sprints

- Forgejo-Adapter als erste Adapter-Implementierung
- Multimodale Ingestion (Bilder, Videos)
- Redis-backed Rate Limiting für Multi-Instance
- AGE Integration-Tests gegen laufende Instanz
