# Testdata Seed Design

**Goal:** Reproduzierbare Testdaten im Repo tracken und per Docker Compose Profil automatisch seeden.

**Architecture:** 21 NovaTech-Testdokumente werden als JSON-Datei in `testdata/` versioniert. Ein Seed-Container liest die Dokumente und schickt sie einzeln durch die Ingestion-API (`POST /ingest`). Ollama generiert die Embeddings live. Der Seed-Container laeuft nur mit `docker compose --profile seed up`.

## Entscheidungen

| Frage | Entscheidung | Grund |
|-------|-------------|-------|
| Embeddings | Live via Ollama | Einfacher zu pflegen, kein Regenerieren bei Modellwechsel |
| Seed-Pfad | Durch Ingestion-API | Testet echten Pfad (PII-Scan, OPA, PostgreSQL-Metadaten) |
| Ausloesung | Docker Compose `--profile seed` | Ein Befehl fuer komplette Testumgebung |
| Datenformat | JSON-Datei im Repo | Reviewbar, diffbar, versioniert |

## Aenderungen

### 1. Ingestion-API: `collection`-Override (ingestion/ingestion_api.py)

Neues optionales Feld `collection` in `IngestRequest`. Wenn gesetzt, wird die `COLLECTION_MAP`-Logik uebersprungen. Noetig weil `source_type=json` sonst immer auf `knowledge_general` mappt, aber Testdaten alle 3 Collections brauchen.

### 2. testdata/documents.json

21 Dokumente extrahiert aus `scripts/seed_comprehensive_testdata.py`:
- `knowledge_general`: 10 Dokumente (HR, Finance, Culture, Legal, Ops)
- `knowledge_code`: 6 Dokumente (Dev Guidelines, API, Security)
- `knowledge_rules`: 5 Dokumente (Governance, Compliance)

Format pro Dokument:
```json
{
  "id": "novatech-onboarding-checklist",
  "collection": "knowledge_general",
  "classification": "public",
  "title": "Onboarding-Checkliste",
  "source": "hr-wiki",
  "project": "novatech-hr",
  "type": "doc",
  "content": "..."
}
```

### 3. testdata/seed.py

Python-Skript (nur stdlib + urllib):
1. Wartet auf Ollama, Ingestion-API, MCP-Server (HTTP health polls)
2. Pullt `nomic-embed-text` Modell falls nicht vorhanden
3. Erstellt Qdrant-Collections falls noetig
4. Iteriert ueber documents.json, ruft `POST /ingest` pro Dokument
5. Verifiziert via MCP `search_knowledge` dass Daten abrufbar sind

### 4. testdata/Dockerfile

Minimales Python-Image, kopiert seed.py + documents.json.

### 5. docker-compose.yml: Seed-Service

```yaml
seed:
  profiles: ["seed"]
  build: ./testdata
  depends_on:
    qdrant: { condition: service_healthy }
    postgres: { condition: service_healthy }
    opa: { condition: service_healthy }
    ingestion: { condition: service_started }
    ollama: { condition: service_started }
  environment:
    INGESTION_URL: http://ingestion:8081
    OLLAMA_URL: http://ollama:11434
    MCP_URL: http://mcp-server:8080
    QDRANT_URL: http://qdrant:6333
```

### 6. Aufraeum: scripts/seed_comprehensive_testdata.py

Wird durch Verweis auf `testdata/` ersetzt. Das alte Skript bleibt als deprecated Referenz oder wird entfernt.

## Nutzung

```bash
# Kompletter Stack mit Testdaten
docker compose --profile seed up -d

# Nur Infrastruktur (ohne Testdaten)
docker compose up -d
```
