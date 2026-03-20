# CLAUDE.md — Wissensdatenbank (KB)

## Projektübersicht

Self-hosted Wissensdatenbank mit MCP-Zugriff, Policy-Engine und Datenschutz.
Agenten greifen ausschließlich über das Model Context Protocol (MCP) zu.
Alle Komponenten sind Open Source und laufen als Docker-Container.

## Architektur

```
Agent/Skill
    │ MCP
    ▼
┌─────────────────────────────────────────────┐
│  MCP-Server (FastAPI)                       │
│  ├─ OPA Policy-Check (jeder Request)        │
│  ├─ Qdrant Vektorsuche (oversampled)        │
│  ├─ Reranker (Cross-Encoder, Top-N)         │
│  ├─ PostgreSQL (strukturierte Daten)         │
│  └─ Audit-Log (DSGVO-konform)               │
└─────────────────────────────────────────────┘
    │           │           │           │
    ▼           ▼           ▼           ▼
 Qdrant    PostgreSQL     OPA       Reranker
 (Vektoren) (Daten+Meta+Vault) (Regeln)  (Cross-Enc.)
    │           │
    ▼           ▼
 Ollama     Forgejo (extern, bestehendes Setup)
 (Embeddings) (Policies, Schemas, Code)
```

## Verzeichnisstruktur

```
kb-project/
├── CLAUDE.md              ← Du bist hier
├── README.md              ← Setup-Anleitung
├── docker-compose.yml     ← Alle Services
├── .env.example           ← Umgebungsvariablen
├── mcp-server/
│   ├── server.py          ← MCP-Server (10 Tools)
│   ├── graph_service.py   ← Knowledge Graph (Apache AGE)
│   ├── Dockerfile
│   └── requirements.txt
├── reranker/
│   ├── service.py         ← Cross-Encoder Service
│   ├── Dockerfile
│   └── requirements.txt
├── ingestion/
│   ├── pii_scanner.py     ← PII-Erkennung (Presidio)
│   ├── retention_cleanup.py ← DSGVO-Löschjobs
│   ├── Dockerfile
│   └── requirements.txt
├── init-db/
│   ├── 001_schema.sql     ← Basis-Schema
│   ├── 002_privacy.sql    ← Datenschutz-Erweiterung
│   ├── 003_knowledge_graph.sql ← Apache AGE Graph-Setup
│   └── 007_pii_vault.sql       ← Sealed Vault (PII-Originale + Mappings)
├── opa-policies/kb/
│   ├── access.rego         ← Zugriffskontrolle
│   ├── rules.rego          ← Business Rules
│   └── privacy.rego        ← DSGVO-Policies
└── docs/
    ├── architektur.md              ← Detaillierte Doku (inkl. Bausteine 3-5, DSGVO §4.5)
    ├── bekannte-schwachstellen.md  ← P0–P3 Issues, Priorisierung, Fix-Hinweise
    ├── technologie-entscheidungen.md ← VLM, vLLM, Git-Adapter, OTel, Adapter-Schicht
    ├── skalierbarkeit.md           ← Load Balancing, Caching, Skalierungsstufen
    └── dsgvo-externe-ki-dienste.md ← Rechtliche Einschätzung claude.ai / externe LLMs
```

## Komponenten und Ports

| Service       | Port  | Technologie                        | Aufgabe                          |
|---------------|-------|------------------------------------|----------------------------------|
| mcp-server    | 8080  | Python, FastAPI, MCP SDK           | Einziger Agenten-Zugangspunkt    |
| reranker      | 8082  | Python, sentence-transformers      | Cross-Encoder Reranking          |
| ingestion     | 8081  | Python, FastAPI                    | ETL, Chunking, Embedding         |
| qdrant        | 6333  | Qdrant                             | Vektordatenbank                  |
| postgres      | 5432  | PostgreSQL 16 + Apache AGE         | Strukturierte Daten + Graph + Audit|
| opa           | 8181  | Open Policy Agent                  | Regelwerk + Zugriffskontrolle    |
| ollama        | 11434 | Ollama                             | Lokale Embeddings                |
| forgejo       | 3000  | Forgejo (extern, nicht im Compose) | Git-Repos, Policies, Schemas     |

## Schlüsselkonzepte

### Datenklassifizierung
Jedes Datenobjekt hat eine Klassifizierungsstufe:
- `public` — Frei zugänglich für alle Agenten
- `internal` — Nur für Rollen analyst, admin, developer
- `confidential` — Nur admin
- `restricted` — Admin + expliziter Zweck

OPA prüft bei **jedem** MCP-Request die Klassifizierung.

### Suchpipeline (3-stufig)
1. **Qdrant** liefert `top_k × 5` Ergebnisse (Oversampling)
2. **OPA** filtert nach Policy und Klassifizierung
3. **Cross-Encoder** bewertet Query-Dokument-Relevanz, gibt Top-K zurück

Wenn der Reranker ausfällt → Graceful Fallback auf Qdrant-Reihenfolge.

### Sealed Vault (Dual Storage)
PII-Daten werden in zwei Stufen gespeichert:
1. **Qdrant** enthält nur pseudonymisierte Texte (deterministisch, per-Projekt-Salt)
2. **pii_vault Schema** (PostgreSQL, RLS) speichert Originale + Mapping

Zugriff auf Originale erfordert:
- HMAC-signiertes Token mit Ablaufzeit
- OPA-Policy-Check (`vault_access_allowed`)
- Zweckbindung (nur erlaubte Purposes)
- Felder werden nach Purpose redaktiert (`vault_fields_to_redact`)

Art. 17 Löschung: Vault-Mapping löschen → Pseudonyme werden irreversibel (restrict-Stufe).

### MCP-Tools (10 Stück)
- `search_knowledge` — Semantische Suche (Qdrant + Reranking); optional: Original-PII via Vault-Token
- `query_data` — Strukturierte Abfragen (PostgreSQL)
- `get_rules` — Business Rules für Kontext abrufen
- `check_policy` — OPA-Policy evaluieren
- `ingest_data` — Neue Daten einspeisen
- `get_classification` — Klassifizierung abfragen
- `list_datasets` — Datensätze auflisten
- `get_code_context` — Code-Suche (Qdrant + Reranking)
- `graph_query` — Knowledge Graph abfragen (Knoten, Beziehungen, Pfade)
- `graph_mutate` — Knowledge Graph verändern (nur developer/admin)

### Datenschutz (DSGVO)
- **PII-Scanner** (Microsoft Presidio) bei Ingestion
- **Zweckbindung** über OPA-Policy (`kb.privacy`)
- **Aufbewahrungsfristen** mit automatischer Löschung
- **Recht auf Löschung** (Art. 17) mit Tracking-Tabelle
- **Audit-Log** für jeden Zugriff auf PII-Daten
- **Sealed Vault** für reversible Pseudonymisierung (Original im Vault, Pseudonym in Qdrant)
- **HMAC-Token** für zeitlich begrenzten Vault-Zugriff
- **2-Tier Löschung** (Art. 17): Vault löschen = Pseudonyme irreversibel

### Forgejo-Integration
Kein eigener Git-Container — nutzt bestehendes Forgejo:
- `kb-policies` Repo → OPA Bundle-Polling
- `kb-schemas` Repo → JSON-Schema-Validierung
- `kb-docs` + Projekt-Repos → Ingestion Pipeline

## Entwicklung

### Voraussetzungen
- Docker + Docker Compose
- Zugang zum bestehenden Forgejo-Server
- Forgejo API-Token mit `read:repository` Berechtigung

### Erststart
```bash
cp .env.example .env
# .env anpassen: PG_PASSWORD, FORGEJO_URL, FORGEJO_TOKEN

docker compose up -d

# Embedding-Modell in Ollama laden
docker exec kb-ollama ollama pull nomic-embed-text

# Qdrant-Collections anlegen
curl -X PUT http://localhost:6333/collections/knowledge_general \
  -H 'Content-Type: application/json' \
  -d '{"vectors":{"size":768,"distance":"Cosine"}}'

curl -X PUT http://localhost:6333/collections/knowledge_code \
  -H 'Content-Type: application/json' \
  -d '{"vectors":{"size":768,"distance":"Cosine"}}'

curl -X PUT http://localhost:6333/collections/knowledge_rules \
  -H 'Content-Type: application/json' \
  -d '{"vectors":{"size":768,"distance":"Cosine"}}'
```

### Healthchecks
```bash
curl http://localhost:6333/healthz        # Qdrant
curl http://localhost:8181/health          # OPA
curl http://localhost:8082/health          # Reranker
curl http://localhost:11434/api/tags       # Ollama
```

### OPA-Policies testen
```bash
# Policy lokal testen
docker exec kb-opa /opa eval \
  -d /policies/kb/access.rego \
  -i '{"agent_role":"analyst","classification":"internal","action":"read"}' \
  'data.kb.access.allow'
```

## Offene Bausteine (Roadmap)

Priorisierte Reihenfolge:

1. ✅ **Reranking** — Cross-Encoder Service (implementiert)
2. ✅ **Knowledge Graph** — Apache AGE (implementiert)
3. ✅ **Evaluation + Feedback-Loop** — `init-db/004_evaluation.sql`, MCP-Tools `submit_feedback`/`get_eval_stats`, `evaluation/run_eval.py`
4. ✅ **Wissens-Versionierung** — `init-db/005_versioning.sql`, `ingestion/snapshot_service.py`, MCP-Tools `create_snapshot`/`list_snapshots`
5. ✅ **Monitoring** — Prometheus + Grafana + Tempo in `docker-compose.yml`, Konfiguration in `monitoring/`

Details zu allen Bausteinen (Architektur, Metriken, Alerting, Tracing): siehe `docs/architektur.md`

## Code-Konventionen

- Python 3.12+, Type Hints überall
- Async/Await für alle I/O-Operationen
- Pydantic-Modelle für Request/Response
- Rego-Policies in `opa-policies/kb/` mit Package `kb.*`
- SQL-Migrationen nummeriert: `001_schema.sql`, `002_privacy.sql`, ...
- Docker-Images: Multi-Stage wo sinnvoll, Alpine-basiert wo möglich
- Umgebungsvariablen für alle Konfiguration (keine Hardcodes)
- Graceful Degradation: Jeder Service muss ohne den Reranker funktionieren

## Wichtige Entscheidungen

| Entscheidung | Gewählt | Alternativen | Grund |
|---|---|---|---|
| Vektordatenbank | Qdrant | Milvus, ChromaDB | Beste Perf. + Filter + Clustering |
| Embedding | nomic-embed-text (768d) | mxbai-embed-large | Balance Qualität/Speed |
| Reranker | ms-marco-MiniLM-L-6-v2 | bge-reranker-v2-m3 | Schnell; Multilingual als Option |
| Policy Engine | OPA (Rego) | Cerbos, GoRules | CNCF-Standard, Battle-tested |
| PII-Scanner | Presidio | spaCy NER | Breite Entity-Erkennung + erweiterbar |
| Git-Server | Forgejo (extern) | Gitea | Bereits vorhanden, API-kompatibel |
| Relationale DB | PostgreSQL 16 | MySQL, SQLite | JSONB, GIN-Index, Extensions |
| PII-Speicherung | Sealed Vault (Dual) | Destructive Masking, Full Encryption | Reversibel, suchbar, DSGVO-konform |
