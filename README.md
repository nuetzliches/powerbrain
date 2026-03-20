# Wissensdatenbank (KB)

Self-hosted Knowledge Base mit MCP-Zugriff, Policy-Engine und DSGVO-Integration.

## MVP-Status

Der aktuelle Fokus ist ein `Search-first`-MVP:

- MCP ist ueber `http://localhost:8080/mcp` erreichbar
- Prometheus-Metriken laufen separat auf Port `9091`
- der minimal abgesicherte Suchpfad ist `MCP -> Ollama -> Qdrant -> OPA -> optionaler Reranker`
- Authentifizierung, Ingestion-API und Snapshot-Flows bleiben vorerst Phase-2-Themen

## Quickstart

```bash
git clone <repo-url> && cd kb-project
cp .env.example .env
# .env editieren: FORGEJO_URL, FORGEJO_TOKEN, PG_PASSWORD

docker compose up -d

# Embedding-Modell laden
docker exec kb-ollama ollama pull nomic-embed-text

# Qdrant-Collections anlegen
for col in knowledge_general knowledge_code knowledge_rules; do
  curl -s -X PUT "http://localhost:6333/collections/$col" \
    -H 'Content-Type: application/json' \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}' && echo " → $col OK"
done
```

## MCP-Server verbinden

In deiner Claude-Konfiguration:

```json
{
  "mcpServers": {
    "wissensdatenbank": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

## Search-first MVP verifizieren

Minimaler Stack:

```bash
docker compose up -d postgres qdrant opa ollama reranker mcp-server
docker exec kb-ollama ollama pull nomic-embed-text
python3 scripts/seed_demo_search_data.py
python3 scripts/smoke_search_first_mvp.py
python3 scripts/smoke_search_first_mvp.py --check-reranker-fallback
```

Die beiden Scripts uebernehmen den MVP-Nachweis:

- `scripts/seed_demo_search_data.py` legt Collections an, erzeugt ein Embedding ueber Ollama und schreibt ein Demo-Dokument nach Qdrant
- `scripts/smoke_search_first_mvp.py` verbindet sich ueber den echten MCP-HTTP-Endpunkt, ruft `search_knowledge` auf und kann optional den Reranker-Fallback pruefen

Der Seed ist in `docs/plans/2026-03-20-search-seed-notes.md` dokumentiert.

## Architektur

Siehe `CLAUDE.md` für das vollständige Architekturkonzept,
`docs/architektur.md` für die detaillierte technische Dokumentation.

## Für Agenten

Agenten greifen auf die Wissensdatenbank ausschließlich über den MCP-Server zu.
Die vollständige Tool-Referenz mit Beispielen, Zugriffslogik und Troubleshooting liegt in:

```
skills/querying-knowledge-base/SKILL.md
```

### Nativer MCP-Zugang (empfohlen)

MCP-Server in der Agent-Konfiguration registrieren:

```json
{
  "mcpServers": {
    "wissensdatenbank": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

Danach stehen alle 14 KB-Tools direkt zur Verfügung (`search_knowledge`, `graph_query`, `check_policy`, etc.).

### Als Skill installieren

Für Agenten ohne nativen MCP-Zugang beschreibt der Skill den HTTP/curl-Zugriff.

- **OpenCode**: `SKILL.md` nach `~/.config/opencode/skills/querying-knowledge-base/` kopieren
- **Claude Code**: Als Custom Command in `.claude/commands/` ablegen
- **Andere**: `SKILL.md` direkt lesen (z.B. via Raw-URL im Git-Repository)

## Lizenz

Alle Eigenentwicklungen: MIT. Abhängigkeiten unter ihren jeweiligen Lizenzen.
