# Wissensdatenbank (KB)

Self-hosted Knowledge Base mit MCP-Zugriff, Policy-Engine und DSGVO-Integration.

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

## Architektur

Siehe `CLAUDE.md` für das vollständige Architekturkonzept,
`docs/architektur.md` für die detaillierte technische Dokumentation.

## Lizenz

Alle Eigenentwicklungen: MIT. Abhängigkeiten unter ihren jeweiligen Lizenzen.
