# Getting Started with Powerbrain

This guide walks you through setting up Powerbrain, ingesting your first data, and running your first search.

## Prerequisites

- Docker and Docker Compose
- ~4 GB RAM for all services
- A terminal

## 1. Setup

Run the automated quickstart:

```bash
git clone https://github.com/nuetzliches/powerbrain.git
cd powerbrain
./scripts/quickstart.sh
```

This will:
- Create `.env` and secrets if missing
- Start all core services (MCP server, Qdrant, PostgreSQL, OPA, Ollama, Reranker)
- Pull the embedding model
- Create Qdrant vector collections
- Verify everything is healthy

## 2. Connect Your Agent

Add Powerbrain to your MCP client configuration:

```json
{
  "mcpServers": {
    "powerbrain": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

This works with Claude Desktop, Claude Code, OpenCode, or any MCP-compatible client.

## 3. Ingest Data

Use the `ingest_data` tool to add content to the knowledge base:

```
Tool: ingest_data
Arguments:
  source: "Our refund policy allows returns within 30 days of purchase. Items must be in original packaging. Digital products are non-refundable."
  project: "customer-support"
  classification: "internal"
  metadata: {"category": "policy", "author": "ops-team"}
```

The ingestion pipeline will:
1. Run PII detection (Microsoft Presidio)
2. Pseudonymize any detected PII
3. Compute quality score (Art. 10 data quality gate)
4. Generate embeddings (nomic-embed-text)
5. Create context layer abstracts (L0/L1)
6. Store in Qdrant + PostgreSQL

## 4. Search

Use `search_knowledge` to find relevant context:

```
Tool: search_knowledge
Arguments:
  query: "What is the return policy?"
  top_k: 5
```

The search pipeline:
1. Embeds your query
2. Qdrant returns 25 candidates (5x oversampling)
3. OPA filters by your agent role and data classification
4. Cross-Encoder reranks by relevance
5. Returns the top 5 results

### With Summarization

```
Tool: search_knowledge
Arguments:
  query: "What is the return policy?"
  summarize: true
  summary_detail: "brief"
```

OPA policies control whether summarization is allowed, required, or denied based on data classification.

### Context Layers

For progressive loading, use the `layer` parameter:

```
Tool: search_knowledge
Arguments:
  query: "return policy"
  layer: "L0"
```

- **L0** — Abstract (~100 tokens): quick overview
- **L1** — Overview (~1-2k tokens): key details
- **L2** — Full chunks (default): complete content

Drill down into a specific document:

```
Tool: get_document
Arguments:
  doc_id: "<doc_id from search results>"
  layer: "L2"
```

## 5. Understand Policies

Powerbrain checks OPA policies on every request. The default configuration:

| Classification | Viewer | Analyst | Developer | Admin |
|---|---|---|---|---|
| public | read | read | read, write | full |
| internal | - | read | read, write | full |
| confidential | - | - | - | full |
| restricted | - | - | - | full + purpose |

Check what a specific role can access:

```
Tool: check_policy
Arguments:
  action: "read"
  resource: "knowledge"
  classification: "internal"
```

Policies are defined in `opa-policies/pb/data.json` and can be modified at runtime via the `manage_policies` tool (admin only).

## 6. Knowledge Graph

Query relationships between entities:

```
Tool: graph_query
Arguments:
  action: "find_node"
  label: "Document"
  properties: {"project": "customer-support"}
```

Find connections:

```
Tool: graph_query
Arguments:
  action: "get_neighbors"
  label: "Document"
  node_id: "<node_id>"
```

## Next Steps

- **[MCP Tool Reference](mcp-tools.md)** — All 23 tools with parameters
- **[Architecture](architecture.md)** — How the components interact
- **[Deployment Guide](deployment.md)** — Production setup, TLS, Docker Secrets
- **[AI Provider Proxy](../README.md#optional-ai-provider-proxy)** — Use Powerbrain transparently with any LLM
