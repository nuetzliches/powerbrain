# Architecture Documentation

## 1. Design Principles

- **Separation of Concerns**: Each component has exactly one responsibility
- **MCP-First**: The MCP server is the sole access point for agents
- **GitOps for Rules**: Business rules as Rego code in any Git repository, versioned and reviewable
- **Local Embeddings**: Any OpenAI-compatible provider (Ollama, vLLM, TEI), no data leaves the infrastructure
- **Graceful Degradation**: Every optional service (reranker) can fail without blocking the system

## 2. Components

### 2.1 Qdrant (Vector Database)

Three collections, each with 768 dimensions (nomic-embed-text):

| Collection | Content | Payload Fields |
|---|---|---|
| `pb_general` | General knowledge, docs | source, type, classification, project, updated_at |
| `pb_code` | Code snippets, API docs | repo, language, path, classification |
| `pb_rules` | Embedded rule sets | rule_id, category, severity, active |

### 2.2 PostgreSQL 16

Core schema (001_schema.sql): datasets, dataset_rows, documents_meta, classifications, agent_access_log, projects.

Privacy extension (002_privacy.sql): data_categories, data_subjects, deletion_requests, pii_scan_log, v_expiring_data.

JSONB + GIN index for flexible schemas. All metadata and structured data is stored here.

### 2.3 OPA (Open Policy Agent)

Three policy packages:

- `pb.access` — Access control based on classification and role
- `pb.rules` — Business rules (pricing, workflow, compliance)
- `pb.privacy` — GDPR: purpose binding, PII handling, retention periods, right to erasure

Policies are polled as a bundle from a Git repository (e.g., `pb-policies`). Any Git server works (Forgejo, GitHub, GitLab, etc.).

### 2.4 Knowledge Graph (Apache AGE)

Apache AGE is a PostgreSQL extension that enables openCypher queries directly within the existing PostgreSQL instance. No separate graph server required.

Node types: Project, Technology, Person, Document, Rule, DataSource.

Relationship types: USES, OWNS, DEPENDS_ON, DOCUMENTS, GOVERNS, SOURCED_FROM.

The graph complements vector search with structured relationships. An agent can ask, for example: "Which technologies does project X use?" or "Who is responsible for all documents affected by rule Y?" — questions that cannot be answered with pure similarity search.

MCP tools: `graph_query` (read, traverse, find paths) and `graph_mutate` (create nodes/relationships, developer/admin only).

### 2.5 Reranker (Configurable Backend)

Evaluates query-document pairs for relevance. Significantly more accurate than cosine similarity, but slower — therefore used as a second stage after Qdrant oversampling.

The reranker backend is configurable via `RERANKER_BACKEND` (default: `powerbrain`):

| Backend | Service | API Format | Use Case |
|---|---|---|---|
| `powerbrain` | Built-in Cross-Encoder (`reranker/service.py`) | Powerbrain `/rerank` | Default, self-hosted, GDPR-safe |
| `tei` | HuggingFace Text Embeddings Inference | TEI `/rerank` | GPU-accelerated, self-hosted |
| `cohere` | Cohere Rerank API v2 | Cohere `/v2/rerank` | Best quality, external SaaS |

**Abstraction:** `shared/rerank_provider.py` implements a strategy pattern with format translation per backend. The MCP server calls `_rerank_provider.rerank()` without knowing which backend is active. Each provider handles request/response mapping (e.g., TEI uses flat `texts[]` array + index-based response mapping, Cohere uses `relevance_score` + model parameter).

**Built-in model options** (when `RERANKER_BACKEND=powerbrain`):
- `cross-encoder/ms-marco-MiniLM-L-6-v2` (default, fast)
- `BAAI/bge-reranker-v2-m3` (multilingual, for DE+EN)

**GDPR note:** External backends (`cohere`, remote `tei`) send document content outside your infrastructure. Ensure compliance with data processing agreements. See `docs/gdpr-external-ai-services.md`.

### 2.6 Embedding & LLM Provider

Any OpenAI-compatible endpoint for embeddings and summarization. Configured via `EMBEDDING_PROVIDER_URL` and `LLM_PROVIDER_URL`. Options:

| Provider | Use Case | Profile |
|---|---|---|
| Ollama (default) | Local CPU inference | `--profile local-llm` |
| vLLM | GPU-accelerated, production | `--profile gpu` |
| HuggingFace TEI | GPU embeddings | `--profile gpu` |
| Any OpenAI-compatible | External or custom | Set provider URL |

Default model: `nomic-embed-text` (768d). For higher accuracy: `mxbai-embed-large` (1024d) — requires adjustment of the Qdrant collection dimension.

### 2.7 Git Server (external, optional)

Any Git server for policy and schema repositories. Configured via `FORGEJO_URL` / `OPAL_POLICY_REPO_URL`. Supports Forgejo, GitHub, GitLab, Gitea, Bitbucket, etc. Repositories:
- `pb-policies` — OPA Rego files
- `pb-schemas` — JSON schemas for datasets
- `pb-docs` — Technical documentation
- `pb-ingestion-config` — ETL templates

### 2.8 GitHub Adapter (Source Adapter)

First implementation of the source adapter framework (`ingestion/adapters/`). Syncs GitHub repository contents into the knowledge base.

**Architecture:**
```
repos.yaml → GitAdapter → GitHubProvider (REST API)
                ↓
         NormalizedDocument
                ↓
    POST /ingest (standard pipeline: PII → OPA → embed → Qdrant)
```

**Sync flow:**
1. pb-worker triggers `POST /sync` on ingestion service (configurable interval)
2. Sync service loads `repo_sync_state` from PostgreSQL (last commit SHA)
3. If first sync: fetch full tree via GitHub Trees API
4. If incremental: compare commits, process only changed files
5. Removed files: cascade-delete from Qdrant, PG, vault, knowledge graph
6. Update sync state with new SHA

**Auth:** PAT (Docker Secret) or GitHub App (JWT → installation token). **Config:** `ingestion/repos.yaml` with per-repo settings (branch, collection, classification, include/exclude patterns).

The adapter framework is extensible — future providers (GitLab, Bitbucket) implement the same `SourceAdapter` interface.

### 2.9 Document Extraction (shared across adapters + chat path)

Binary documents (PDF, DOCX, XLSX, PPTX, MSG, EML, RTF) are converted to text
by a shared `ContentExtractor` in `ingestion/content_extraction/`, primarily
via Microsoft [markitdown](https://github.com/microsoft/markitdown) with
per-format fallbacks (`python-docx`, `openpyxl`, `python-pptx`).

Three consumers share the extractor:

1. **Office 365 adapter** — original user, unchanged behavior (the module it
   used to host was moved up; `office365/content.py` is now a back-compat shim).
2. **GitHub adapter** — opt-in per repo via `allow_documents: true` in
   `repos.yaml`. Fetches documents as bytes (`get_file_bytes`) and extracts
   them before ingestion (`source_type="github-document"`).
3. **pb-proxy chat path** — when a user attaches a file/document block to a
   chat message (`/v1/chat/completions` or `/v1/messages`), the proxy calls
   `POST /extract` on the ingestion service and replaces the block with the
   extracted text so the PII pseudonymizer and the LLM see plain text.

**Policy gates** (OPA `pb.proxy.documents`):

| Key | Default | Purpose |
|---|---|---|
| `allowed_roles` | `analyst`, `developer`, `admin` | Roles allowed to attach docs |
| `max_bytes` | 25 MB | Per-file size cap |
| `allowed_mime_types` | 15 canonical types | Explicit MIME allowlist |
| `max_files_per_request` | 3 (default) / 10 (elevated) | Prevents abuse via many attachments |

**OCR fallback** (opt-in) for scanned PDFs: build the ingestion image with
`--build-arg WITH_OCR=true` (adds Tesseract + poppler, ~120 MB) and set
`OCR_FALLBACK_ENABLED=true` at runtime. The fallback kicks in when a PDF's
extracted text is below `OCR_FALLBACK_MIN_CHARS` (default 50).

## 3. Search Pipeline

```
Query → Embedding (configurable provider) → Qdrant (top_k × 5)
  → OPA Policy Filter → Reranker (configurable backend) → Top-K Results
```

Oversampling factor: 5. With top_k=10, Qdrant fetches 50 results, OPA filters to e.g. 35, the reranker selects the 10 most relevant.

## 4. Privacy

### 4.1 PII at Ingestion

Presidio scans incoming data. OPA policy decides:
- `public` → mask PII (`Max Mustermann` → `<PERSON>`)
- `internal` → pseudonymize PII (deterministic, reversible with salt)
- `confidential` → store encrypted + document legal basis
- `restricted` → PII data is not ingested

### 4.2 Purpose Binding

Every MCP request includes `purpose`. OPA checks against allowed processing purposes per data category. Reporting agents do not see PII fields (data minimization via `fields_to_redact`).

### 4.3 Retention

Cronjob (`retention_cleanup.py`) checks `retention_expires_at` and deletes coordinately from PostgreSQL + Qdrant. Audit logs are anonymized (not deleted — proof of compliance required).

### 4.4 Deletion Requests (Art. 17)

Table `deletion_requests` + `data_subjects`. The cleanup service checks statutory retention obligations before deleting. Status tracking: pending → processing → completed/blocked.

### 4.5 Privacy Incidents — LLM Detection and Reporting Obligations (Art. 33/34)

**Question:** Is it useful to log GDPR violations discovered by LLMs
("sweeping it under the rug" vs. documenting)?

#### Legal Assessment

**Documenting is mandatory — concealing is dramatically more costly.**

The GDPR legal situation is clear:

| Norm | Requirement |
|------|-------------|
| Art. 5(2) | Accountability: the controller must be able to **demonstrate** compliance |
| Art. 33 | Notification to supervisory authority **within 72 hours** of becoming aware |
| Art. 34 | Notification of affected persons in case of high risk |
| Art. 83(4) | Fine up to €10 million / 2% of annual turnover for Art. 33 violations |
| §42 BDSG | Criminal liability (up to 3 years imprisonment) for willful non-reporting |

The moment of "becoming aware" (Art. 33 para. 1) begins as soon as the system
detects the incident — not when a human evaluates it. LLM detection
= becoming aware.

**Practice of supervisory authorities (BfDI, LfDI):** Concealed incidents are uncovered during
later audits, data protection impact assessments, or complaints.
Authorities then impose 2–4× higher fines than for proactive reporting.
Documented good faith is the strongest mitigating factor for fines (Art. 83 para. 2 lit. b, c).

**Not logging** provides no protection whatsoever, but instead:
- Prevents the 72h deadline from being triggered in time (even if you report
  later, you are already in default)
- Destroys the evidence that the system is operated according to the state of the art
- Makes the DPO and potentially management personally liable (§42 BDSG)

#### What an LLM Can Detect

Presidio (ingestion scanner) has false negatives — especially for:
- Implicitly identifying combinations (name + employer + place of residence, none
  of the three items alone is PII)
- Context-dependent information (pseudonym that is internally assigned to a person)
- Non-standard formats (e.g., customer numbers that structurally resemble a name)

When an LLM detects PII in a document where it should not be present,
it means: **the PII has already been embedded in Qdrant, potentially
logged in audit logs, and returned to other agents.** This is a
data breach under Art. 4 No. 12 GDPR.

#### Implementation: `privacy_incidents` Table (`006_privacy_incidents.sql`)

Status workflow:
```
detected → under_review → contained → notified_authority (if reportable)
                                    → resolved (no reporting requirement)
         → false_positive (false alarm after review)
```

Key properties:
- Status history is automatically written via trigger (append-only audit trail)
- View `v_incidents_requiring_attention` warns 24h/48h before deadline expiry
- Index on `notifiable_risk = true AND authority_notified_at IS NULL` for
  fast access to open reporting obligations
- Table must never be truncated (statutory proof requirement)

#### Recommended Process for LLM Detection

1. **Automatically (MCP tool or agent):** `INSERT INTO privacy_incidents`
   with `source = 'llm_detection'`, `status = 'detected'`
2. **Immediately:** Set affected datasets/documents to `classification = 'restricted'`
   → OPA blocks access
3. **Within 24h:** Human review: false positive? → `false_positive`.
   Actual PII? → `contained` + assessment of `notifiable_risk`
4. **Within 72h:** If `notifiable_risk = true` → report to BfDI/LfDI,
   set `authority_notified_at`
5. **Data deletion:** Remove PII from Qdrant (delete vectors), pseudonymize PG fields,
   set `resolved`

#### Terminology

The system uses **`quarantined`** internally (access blocked, under review)
instead of "leaked" — "leaked" implies external exfiltration, which is not always the case
and creates unnecessary panic for internal PII detections. "Leaked" is the
worst-case finding after review, not the initial state.

## 5. Scaling

- MCP server + ingestion: Stateless, horizontally scalable (Docker replicas)
- Reranker: Stateless, CPU-intensive — independently scalable from the MCP server
- Qdrant: Native clustering with sharding + replication
- PostgreSQL: PgBouncer for connection pooling, Citus for horizontal sharding
- OPA: In-memory policy evaluation, bundle caching

## 6. Evaluation + Feedback Loop

Goal: measurable retrieval quality. Agents rate results → poorly performing queries are identified.

### 6.1 Database Schema (`004_evaluation.sql`)

- **`search_feedback`** — Rating 1–5 per query, including which result IDs were helpful/irrelevant, rerank scores at the time of feedback
- **`eval_test_set`** — Ground-truth queries with `expected_ids` and `expected_keywords` for offline evaluation
- **`eval_runs`** — Stored evaluation runs with precision, recall, MRR, latency, and per-query details

### 6.2 MCP Tools

- **`submit_feedback`** — Agent rates a search (rating 1–5, optional relevant_ids / irrelevant_ids / comment). Returns: `{ feedback_id, stored: true }`
- **`get_eval_stats`** — Statistics for a time period (default 30 days): avg_rating, satisfaction_pct, top 10 worst queries, trend vs. previous period

### 6.3 Feedback Loop

In the MCP server `search_knowledge`: every search request checks whether the query has an average < 2.5 with ≥ 3 feedbacks in `search_feedback`. If so, a warning is logged. Long-term: increase `OVERSAMPLE_FACTOR` for affected queries.

### 6.4 Offline Evaluator (`evaluation/run_eval.py`)

Standalone script (cronjob, e.g. weekly):
1. Reads `eval_test_set` from PostgreSQL
2. Executes each query directly against Qdrant + reranker
3. Calculates per query: Precision@K, Recall@K, MRR, keyword coverage, latency
4. Aggregates and stores in `eval_runs`
5. Compares with the last run → regression alert for >10% degradation

```bash
python evaluation/run_eval.py                    # full evaluation
python evaluation/run_eval.py --dry-run          # only print, do not store
python evaluation/run_eval.py --collection code  # only one collection
```

---

## 7. Knowledge Versioning

Goal: knowledge state reconstructable at any point in time. Important for compliance evidence, debugging, and rollback after faulty ingestion.

### 7.1 Database Schema (`005_versioning.sql`)

- **`knowledge_snapshots`** — Snapshot metadata: name, timestamp, creator, `components` JSONB (Qdrant snapshot IDs, PG row counts, OPA policy commit hash)
- **`datasets_history`** — SCD Type 2: every change to `datasets` is automatically recorded via trigger with `valid_from`/`valid_to`

### 7.2 Snapshot Service (`ingestion/snapshot_service.py`)

Functions:
- `create_snapshot(name, description)` — Creates Qdrant snapshots for all collections via the native API (`POST /collections/{name}/snapshots`), stores PG row counts and the current Git policy commit hash in `knowledge_snapshots`
- `list_snapshots(limit)` — All snapshots with metadata from PostgreSQL
- `cleanup_old_snapshots(keep_last_n=10)` — Deletes Qdrant snapshots + PG entries beyond the keep limit

Usable as CLI:
```bash
python ingestion/snapshot_service.py --auto          # daily snapshot + cleanup
python ingestion/snapshot_service.py --list          # list snapshots
python ingestion/snapshot_service.py --name my-snap  # create named snapshot
```

### 7.3 MCP Tools

- **`create_snapshot`** — Admin only. Delegates to the ingestion service. Returns: `{ snapshot_id, components, created_at }`
- **`list_snapshots`** — List snapshots from `knowledge_snapshots`, paginated via `limit`

---

## 8. Monitoring + Observability

### 8.1 Infrastructure

| Service | Port | Purpose |
|---|---|---|
| Prometheus | 9090 | Collect metrics + alerting rules |
| Grafana | 3001 | Dashboards + Alertmanager UI |
| Grafana Tempo | 3200 / 4317 | Distributed tracing (OTLP gRPC) |
| postgres-exporter | 9187 | PostgreSQL metrics for Prometheus |

### 8.2 Metrics per Service

**MCP Server** (`mcp-server:8080/metrics`, Prometheus HTTP server):
- `pb_mcp_requests_total{tool, status}` — Requests per tool and status
- `pb_mcp_request_duration_seconds{tool}` — Latency histogram per tool
- `pb_mcp_policy_decisions_total{result}` — OPA allow/deny counter
- `pb_mcp_search_results_count{collection}` — Histogram of result counts
- `pb_mcp_rerank_fallback_total` — Fallbacks when reranker is unreachable
- `pb_feedback_avg_rating` — Gauge: current feedback average (last 24h)

**Reranker** (`reranker:8082/metrics`):
- `pb_reranker_requests_total{status}`
- `pb_reranker_duration_seconds` — Histogram
- `pb_reranker_batch_size` — Histogram of batch sizes
- `pb_reranker_model_load_seconds` — Model load time at startup

### 8.3 Grafana Dashboards

Three preconfigured dashboards in `monitoring/grafana-dashboards/`:
1. **KB Overview** — Requests/min, latency p50/p95/p99, error rate, policy decisions
2. **Search Quality** — Reranker usage, fallback rate, search result histograms
3. **Infrastructure** — Service health, PG connections, tool volume

### 8.4 Alerting (`monitoring/alerting_rules.yml`)

| Alert | Condition | Severity |
|---|---|---|
| HighErrorRate | Error rate > 5% for 5min | warning |
| HighSearchLatency | search p95 > 2s for 10min | warning |
| RerankerDown | `up{job="reranker"} == 0` for 2min | critical |
| LowSearchQuality | `pb_feedback_avg_rating < 2.5` for 1h | warning |
| QdrantDown / PostgresDown | Targets unreachable for 2min | critical |
| HighRerankerFallbackRate | Fallback rate > 10% for 5min | warning |

### 8.5 OpenTelemetry Tracing

Optional via `OTEL_ENABLED=true` in the MCP server. Traces are sent via OTLP gRPC to Grafana Tempo (`http://tempo:4317`). Every MCP tool call creates a span, with child spans for OPA, Qdrant, reranker, and embedding.

---

## 9. Roadmap

1. ✅ Reranking (Cross-Encoder, configurable backend via `shared/rerank_provider.py`)
2. ✅ Knowledge Graph (Apache AGE as PG extension)
3. ✅ Evaluation + Feedback Loop (`evaluation/run_eval.py`, MCP tools `submit_feedback`/`get_eval_stats`)
4. ✅ Knowledge Versioning (`ingestion/snapshot_service.py`, MCP tools `create_snapshot`/`list_snapshots`)
5. ✅ Monitoring (Prometheus + Grafana + Tempo, `monitoring/`)

---

### Context Layers (L0/L1/L2)

Each document is stored at three context layers during ingestion:

| Layer | Content | Tokens | Purpose |
|-------|---------|--------|---------|
| L0 | Abstract (1 sentence) | ~100 | Fast relevance check |
| L1 | Markdown overview | ~500 | Decision whether full text is needed |
| L2 | Full-text chunks | variable | Detailed information (previous behavior) |

**Process:**
1. Ingestion generates L2 chunks (as before)
2. LLM generates L0 (abstract) and L1 (overview) from the L2 chunks
3. All three layers are stored as separate Qdrant points with `layer` payload
4. Agents can query a specific layer using the `layer` parameter

**MCP Integration:**
- `search_knowledge` and `get_code_context`: optional `layer` parameter (L0/L1/L2)
- `get_document`: drill-down from L0 → L1 → L2 via `doc_id`

**Access Control:**
Layers are a progressive loading mechanism, not a security layer.
The existing `pb.access` policy controls whether an agent may view a document.
`pb.summarization` controls whether raw text or only summaries are permitted.
Any agent with access can query any layer level.

**Configuration:**
- `LAYER_GENERATION_ENABLED` (default: `true`) — feature flag
- `LLM_MODEL` (default: `qwen2.5:3b`) — model for L0/L1 generation
