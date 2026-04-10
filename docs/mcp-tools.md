# MCP Tool Reference

Powerbrain exposes 23 tools via the Model Context Protocol. All tools require authentication (valid `pb_` API key). Access is further controlled by OPA policies based on agent role and data classification.

## Search & Retrieval

### `search_knowledge`
Semantic search over the knowledge base with 3-stage pipeline (Qdrant oversampling, OPA filtering, Cross-Encoder reranking).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | yes | — | Search query text |
| `collection` | string | no | `pb_general` | `pb_general`, `pb_code`, or `pb_rules` |
| `filters` | object | no | — | Qdrant metadata filters |
| `top_k` | integer | no | 10 | Number of results to return |
| `layer` | string | no | all | Context layer: `L0` (abstract), `L1` (overview), `L2` (full) |
| `summarize` | boolean | no | false | Request LLM summary of results |
| `summary_detail` | string | no | `standard` | `brief`, `standard`, or `detailed` |
| `rerank_query` | string | no | — | Alternative query text used only for reranking |
| `rerank_options` | object | no | — | Heuristic boost config (see below) |
| `pii_access_token` | object | no | — | HMAC-signed token for vault PII access |
| `purpose` | string | no | — | Purpose for PII access (required with token) |

**Rerank options:** `boost_same_project` (number), `boost_same_author` (number), `match_project` (string), `match_author` (string), `boost_file_overlap` (number), `match_files` (string[]), `boost_corrections` (number).

**Access:** All authenticated roles. Results filtered by OPA based on role + classification.

---

### `get_code_context`
Semantic search over code embeddings.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | yes | — | Code search query |
| `repo` | string | no | — | Filter by repository |
| `language` | string | no | — | Filter by programming language |
| `top_k` | integer | no | 5 | Number of results |
| `layer` | string | no | all | Context layer: `L0`, `L1`, `L2` |
| `summarize` | boolean | no | false | Request summary |
| `summary_detail` | string | no | `standard` | Summary detail level |

**Access:** All authenticated roles.

---

### `get_document`
Retrieve a specific document by ID at a given context layer. Enables progressive loading.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `doc_id` | string | yes | — | Document ID (from search result metadata) |
| `layer` | string | no | `L1` | `L0`, `L1`, or `L2` |
| `collection` | string | no | `pb_general` | Collection to search in |

**Access:** All authenticated roles. OPA checks classification.

---

## Structured Data

### `query_data`
Structured query against PostgreSQL datasets.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `dataset` | string | yes | — | Dataset name |
| `conditions` | object | no | — | Filter conditions |
| `limit` | integer | no | 50 | Max rows |

**Access:** All authenticated roles. OPA checks dataset classification.

---

### `list_datasets`
List available datasets, filtered by OPA access policy.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `project` | string | no | — | Filter by project |
| `source_type` | string | no | — | Filter by source type |

**Access:** All authenticated roles.

---

### `get_classification`
Query the classification level of a resource.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `resource_id` | string | yes | — | Resource identifier |
| `resource_type` | string | yes | — | `dataset`, `document`, or `rule` |

**Access:** All authenticated roles.

---

## Data Management

### `ingest_data`
Ingest new data into the knowledge base. Runs PII scanning, quality scoring, embedding, and context layer generation.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | yes | — | Content to ingest |
| `source_type` | string | no | `text` | Source type identifier |
| `project` | string | no | — | Project association |
| `classification` | string | no | `internal` | Data classification level |
| `metadata` | object | no | `{}` | Custom metadata |

**Access:** All authenticated roles.

---

### `delete_documents`
Bulk-delete documents by filter. Cascades to Qdrant, PostgreSQL, PII Vault, and Knowledge Graph.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `confirm` | boolean | yes | — | Safety flag, must be `true` |
| `source_type` | string | no | — | Filter by source type |
| `project` | string | no | — | Filter by project |
| `delete_all` | boolean | no | false | Delete ALL documents |

**Access:** All authenticated roles (OPA checked).

---

## Knowledge Graph

### `graph_query`
Query the knowledge graph (Apache AGE). Results are PII-scanned before returning.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | yes | — | `find_node`, `find_relationships`, `get_neighbors`, `find_path`, `get_subgraph` |
| `label` | string | no | — | Node label |
| `node_id` | string | no | — | Node identifier |
| `properties` | object | no | — | Property filters |
| `rel_type` | string | no | — | Relationship type filter |
| `to_label` | string | no | — | Target node label |
| `to_id` | string | no | — | Target node ID |
| `max_depth` | integer | no | 2 | Max traversal depth |
| `direction` | string | no | `both` | `out`, `in`, or `both` |

**Access:** All authenticated roles.

---

### `graph_mutate`
Create or delete nodes and relationships. Results are PII-scanned.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | yes | — | `create_node`, `delete_node`, `create_relationship` |
| `label` | string | no | — | Node label |
| `node_id` | string | no | — | Node ID (for delete) |
| `properties` | object | no | — | Node properties |
| `from_label` | string | no | — | Source node label (relationships) |
| `from_id` | string | no | — | Source node ID |
| `to_label` | string | no | — | Target node label |
| `to_id` | string | no | — | Target node ID |
| `rel_type` | string | no | — | Relationship type |
| `rel_properties` | object | no | — | Relationship properties |

**Access:** Developer and Admin only.

---

## Policy & Rules

### `get_rules`
Retrieve active business rules for a context.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `category` | string | yes | — | Rule category |
| `context` | object | no | — | Additional context for rule matching |

**Access:** All authenticated roles.

---

### `check_policy`
Evaluate an OPA policy decision.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | yes | — | Action to check (e.g., `read`, `write`) |
| `resource` | string | yes | — | Resource type |
| `classification` | string | yes | — | Data classification level |

**Access:** All authenticated roles.

---

### `manage_policies`
Read or update OPA policy data sections at runtime. Validates against JSON Schema before writes.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | yes | — | `list`, `read`, or `update` |
| `section` | string | no | — | Config section name (for read/update) |
| `data` | any | no | — | New value (for update) |

**Access:** Admin only.

---

## Evaluation & Feedback

### `submit_feedback`
Rate search result quality for the feedback loop.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | yes | — | Original search query |
| `result_ids` | string[] | yes | — | IDs of received results |
| `rating` | integer | yes | — | 1 (poor) to 5 (excellent) |
| `relevant_ids` | string[] | no | — | IDs of helpful results |
| `irrelevant_ids` | string[] | no | — | IDs of unhelpful results |
| `comment` | string | no | — | Free-text comment |
| `collection` | string | no | — | Collection searched |
| `rerank_scores` | object | no | — | Reranker scores for analysis |

**Access:** All authenticated roles.

---

### `get_eval_stats`
Retrieval quality statistics with windowed metrics.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `days` | integer | no | 30 | Evaluation period in days |

**Access:** All authenticated roles.

---

## Snapshots

### `create_snapshot`
Create a knowledge snapshot (Qdrant + PostgreSQL + OPA policy state).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | yes | — | Snapshot name |
| `description` | string | no | — | Description |

**Access:** Admin only.

---

### `list_snapshots`
List available knowledge snapshots.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | no | 10 | Max results |

**Access:** All authenticated roles.

---

## EU AI Act Compliance

### `generate_compliance_doc`
Generate EU AI Act Annex IV technical documentation as Markdown from live system state.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `output_mode` | string | no | `inline` | `inline` (in response) or `file` (writes to disk) |

**Access:** Admin only.

---

### `verify_audit_integrity`
Verify the tamper-evident SHA-256 hash chain of the audit log (Art. 12).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_id` | integer | no | — | First ID to verify |
| `end_id` | integer | no | — | Last ID to verify |

**Access:** Admin only.

---

### `export_audit_log`
Export audit log entries for compliance review (Art. 12).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `format` | string | no | `json` | `json` or `csv` |
| `since` | string | no | — | ISO-8601 lower bound |
| `until` | string | no | — | ISO-8601 upper bound |
| `agent_id` | string | no | — | Filter by agent |
| `action` | string | no | — | Filter by action |
| `limit` | integer | no | — | Max rows (capped by OPA config) |

**Access:** Admin only.

---

### `get_system_info`
Transparency report (Art. 13): active models, OPA policies, collection stats, PII config, audit integrity.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|

No parameters.

**Access:** All authenticated roles.

---

### `review_pending`
List or decide pending human oversight reviews (Art. 14).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `action` | string | no | `list` | `list`, `approve`, or `deny` |
| `review_id` | string | no | — | UUID of review to decide |
| `reason` | string | no | — | Justification for decision |
| `limit` | integer | no | 50 | Max rows for list |

**Access:** Admin only.

---

### `get_review_status`
Poll the status of a pending human oversight review.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `review_id` | string | yes | — | UUID from original tool call |

**Access:** All authenticated roles (own reviews); Admin (all reviews).
