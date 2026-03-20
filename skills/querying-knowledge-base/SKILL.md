---
name: querying-knowledge-base
description: Use when searching company knowledge, querying internal documentation, checking policies, or accessing the Powerbrain knowledge base via MCP
---

# Querying the Knowledge Base

## Overview

Query the Powerbrain Wissensdatenbank (knowledge base) via its MCP server. The server exposes 14 tools for semantic search, structured queries, policy checks, graph queries, and data ingestion. All requests use **JSON-RPC 2.0 over HTTP POST** (MCP Streamable HTTP transport).

## Server

- **URL**: `http://localhost:8080/mcp`
- **Transport**: MCP Streamable HTTP (JSON-RPC 2.0 over HTTP POST)

## Zugriffswege

### Option A: Nativer MCP-Server (empfohlen)

Wenn dein Agent MCP nativ unterstützt (Claude Code, OpenCode, Cursor, etc.), registriere den Server in deiner Agent-Konfiguration:

**Claude Code** (`~/.claude/mcp_servers.json` oder Projekt-`.mcp.json`):
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

**OpenCode** (`~/.config/opencode/config.json`):
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

Danach stehen alle KB-Tools direkt als MCP-Tools zur Verfügung — kein curl nötig. Der Agent kann `search_knowledge`, `graph_query` etc. wie jedes andere Tool aufrufen.

### Option B: HTTP/curl (ohne native MCP-Integration)

Falls der Agent keinen nativen MCP-Zugang hat, können alle Tools per HTTP POST aufgerufen werden.

**Headers**: `Content-Type: application/json`, `Accept: application/json, text/event-stream`

#### 1. Initialize (einmal pro logischer Session)

```bash
curl -s http://localhost:8080/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "agent", "version": "1.0"}
    }
  }'
```

#### 2. Tool aufrufen

```bash
curl -s http://localhost:8080/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
    "params": {
      "name": "search_knowledge",
      "arguments": {
        "query": "Homeoffice Regelung",
        "collection": "knowledge_general",
        "top_k": 5,
        "agent_id": "my-agent",
        "agent_role": "analyst"
      }
    }
  }'
```

Response enthält `result.content[0].text` mit JSON-Payload.

## Tools Reference

### search_knowledge (semantic search)

Most-used tool. Searches Qdrant vectors with OPA policy filtering and cross-encoder reranking.

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `query` | yes | | Natural language search query |
| `collection` | no | `knowledge_general` | `knowledge_general`, `knowledge_code`, or `knowledge_rules` |
| `top_k` | no | 10 | Number of results to return |
| `agent_id` | yes | | Identifier for the calling agent |
| `agent_role` | yes | | `viewer`, `analyst`, `developer`, or `admin` |
| `project` | no | | Filter by project name |

**Collections:**
- `knowledge_general` — Company docs, HR, policies, processes
- `knowledge_code` — Code guidelines, architecture, API docs
- `knowledge_rules` — Business rules, compliance, data governance

**Roles and access levels:**
- `viewer` — Only `public` documents
- `analyst` — `public` + `internal`
- `developer` — Like analyst + code repos + graph mutations
- `admin` — Full access including `confidential` and `restricted`

**Response structure:**
```json
{
  "results": [
    {
      "id": "uuid",
      "score": 0.57,
      "rerank_score": 0.64,
      "rank": 1,
      "content": "Full document text...",
      "metadata": {
        "title": "Document Title",
        "classification": "internal",
        "source": "hr-wiki",
        "project": "novatech-hr",
        "type": "doc"
      }
    }
  ],
  "total": 3
}
```

### get_code_context (code search)

Like `search_knowledge` but defaults to `knowledge_code` collection. Same parameters.

### check_policy (OPA policy check)

All parameters are required and flat (no wrapper object).

```json
{
  "name": "check_policy",
  "arguments": {
    "action": "read",
    "resource": "document",
    "classification": "confidential",
    "agent_id": "my-agent",
    "agent_role": "viewer"
  }
}
```

Returns `{"allowed": true/false, ...}`.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | yes | `read` or `write` |
| `resource` | yes | Resource type (e.g., `document`) |
| `classification` | yes | `public`, `internal`, `confidential`, `restricted` |
| `agent_id` | yes | Calling agent identifier |
| `agent_role` | yes | `viewer`, `analyst`, `developer`, `admin` |

### query_data (SQL queries)

Runs read-only SQL against PostgreSQL.

```json
{
  "name": "query_data",
  "arguments": {
    "query": "SELECT * FROM datasets LIMIT 5",
    "agent_id": "my-agent",
    "agent_role": "analyst"
  }
}
```

### graph_query (knowledge graph)

Query Apache AGE knowledge graph for entities and relationships.

```json
{
  "name": "graph_query",
  "arguments": {
    "query_type": "neighbors",
    "params": {"label": "Technology", "property_key": "name", "property_value": "PostgreSQL"},
    "agent_id": "my-agent",
    "agent_role": "developer"
  }
}
```

Query types: `neighbors`, `path`, `subgraph`, `search`.

### graph_mutate (modify graph, developer/admin only)

```json
{
  "name": "graph_mutate",
  "arguments": {
    "mutation_type": "add_node",
    "params": {"label": "Technology", "properties": {"name": "Redis", "version": "7.0"}},
    "agent_id": "my-agent",
    "agent_role": "developer"
  }
}
```

Mutation types: `add_node`, `add_edge`, `update_node`, `delete_node`, `delete_edge`.

### Other tools

| Tool | Purpose |
|------|---------|
| `get_rules` | Business rules for a context |
| `get_classification` | Classification level of a document |
| `list_datasets` | List available datasets |
| `ingest_data` | Ingest new data into the KB |
| `submit_feedback` | Submit quality feedback on search results |
| `get_eval_stats` | Get evaluation statistics |
| `create_snapshot` | Create a versioned snapshot |
| `list_snapshots` | List available snapshots |

## Common Patterns

### Search then verify access

```bash
# 1. Check if role has access
curl -s http://localhost:8080/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"agent","version":"1.0"}}}'

# 2. Search
curl -s http://localhost:8080/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"Gehaltsbänder Vergütung","collection":"knowledge_general","agent_id":"hr-bot","agent_role":"admin","top_k":3}}}'
```

### Multi-collection search

For questions spanning multiple topics, search each collection separately and combine results. Rerank scores are comparable within a collection but NOT across collections.

```bash
# Search rules collection
curl -s http://localhost:8080/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"Datenschutz DSGVO","collection":"knowledge_rules","top_k":3,"agent_id":"my-agent","agent_role":"analyst"}}}'

# Search general docs
curl -s http://localhost:8080/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"Datenschutz DSGVO","collection":"knowledge_general","top_k":3,"agent_id":"my-agent","agent_role":"analyst"}}}'
```

Present results grouped by collection or merged by relevance to the user's question.

## Access Control Behavior

The search pipeline enforces access silently. Documents above the agent's clearance are **filtered out without error** — the response simply contains fewer results. A `viewer` searching `knowledge_general` will only see `public` documents, even if the collection contains `internal` and `confidential` ones. You may receive fewer than `top_k` results because of policy filtering.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Empty results | Wrong collection | Try all 3 collections |
| `allowed: false` | Role too low for classification | Use higher role or search `public` docs |
| Connection refused | MCP server not running | `docker compose up -d mcp-server` |
| Timeout on search | Ollama embedding slow (CPU) | Wait, or check `docker logs kb-ollama` |
| Reranker scores missing | Reranker unhealthy | Works without it (graceful fallback) |
