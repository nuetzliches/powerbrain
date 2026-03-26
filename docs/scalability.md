# Scalability & Load Balancing

Architectural considerations for multi-agent scenarios with high parallel load.

---

## Current Bottlenecks (As-Is Analysis)

Before horizontal scaling helps, the per-request bottlenecks must be fixed.
Otherwise you just scale the problem.

### Per-Request Bottlenecks (must be fixed first)

| Problem | Impact | Fix |
|---------|--------|-----|
| **50 serial OPA calls** per search request (P2-1) | 1–2.5s overhead from policy checks alone | OPA batch or Qdrant filter |
| **Embedding sequential** (Ollama, one request after another) | With 10 parallel agents, all wait for each other | Embedding cache + batch API |
| **asyncpg pool too small** (min=2, max=10) | Connection exhaustion from ~5 parallel agents | Pool size + PgBouncer |
| **MCP transport stdio** (P0-1) | No network access possible | HTTP transport (prerequisite for everything) |

### Infrastructure Bottlenecks (addressed by horizontal scaling)

| Component | Bottleneck Type | Strategy |
|-----------|----------------|----------|
| MCP Server | CPU/IO-bound | Horizontal (stateless) |
| Ollama (Embeddings) | GPU/CPU-bound, sequential | Batch API + cache + multiple instances |
| vLLM / HF TEI | GPU-bound, internally batched | Multiple instances |
| Reranker | CPU/GPU-bound | Horizontal (stateless) |
| Qdrant | IO/Memory-bound | Native cluster + sharding |
| PostgreSQL | IO-bound | PgBouncer + read replica |
| OPA | CPU, in-memory | Horizontal trivial |

---

## Target Architecture (Multi-Agent, High Load)

```
                     Agents (n × parallel)
                           │
                    ┌──────┴──────┐
                    │  Load       │
                    │  Balancer   │  (Traefik / Nginx / Envoy)
                    │  + Rate     │  Rate-limit per agent ID
                    │  Limiting   │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    MCP-Server 1     MCP-Server 2     MCP-Server N    (stateless, scalable)
          │                │                │
          └────────────────┼────────────────┘
                           │
        ┌──────────────────┼──────────────────────┐
        │                  │                      │
        ▼                  ▼                      ▼
┌─────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ Embedding   │   │   Qdrant        │   │   PostgreSQL    │
│ Cache       │   │   Cluster       │   │   + PgBouncer   │
│ (Redis)     │   │   (Sharding +   │   │   + Read Replica│
└──────┬──────┘   │   Replication)  │   └─────────────────┘
       │          └─────────────────┘
       ▼
┌──────────────────────────────────┐
│  Embedding Service Pool          │
│  ├─ HF TEI 1 (GPU 0)             │
│  ├─ HF TEI 2 (GPU 1)             │  ← or vLLM multi-GPU
│  └─ Ollama (CPU Fallback)        │
└──────────────────────────────────┘

        ┌────────────────────┐
        │  Reranker Pool     │
        │  ├─ Reranker 1     │
        │  └─ Reranker 2     │
        └────────────────────┘

        ┌────────────────────┐
        │  OPA Pool          │
        │  ├─ OPA 1          │  ← all share the same bundle
        │  └─ OPA 2          │
        └────────────────────┘
```

---

## Component Strategy in Detail

### MCP Server (after P0-1 fix)

Fully stateless — no local state, everything in PG/Qdrant.
Easily horizontally scalable.

```yaml
# docker-compose.yml (with replicas)
mcp-server:
  deploy:
    replicas: 3
    resources:
      limits:
        cpus: '1.0'
        memory: 512M

# OR: Kubernetes HPA
# autoscaling: min=2, max=10, cpu-threshold=60%
```

**Prometheus metrics with multiple instances:**
Each instance exposes `/metrics` on its own port. Prometheus scrapes
all instances, differentiating via the `instance` label. No pushgateway needed.

**Important:** Adjust asyncpg pool size to the number of replicas:
```
max_connections (PG) = replicas × pool_max + overhead
# 3 replicas × 10 = 30 → PG max_connections: 50 is sufficient
# With PgBouncer: no direct relation anymore (see below)
```

---

### Embedding Cache (highest ROI)

Same query → identical vector. Cache hit rate in practice: 40–70%
(similar requests from agents, repeated lookups).

```python
# mcp-server/embedding_cache.py

import hashlib
import json
import redis.asyncio as redis

class EmbeddingCache:
    def __init__(self, redis_url: str, ttl_seconds: int = 3600):
        self.client = redis.from_url(redis_url)
        self.ttl    = ttl_seconds

    def _key(self, model: str, text: str) -> str:
        h = hashlib.sha256(f"{model}:{text}".encode()).hexdigest()
        return f"emb:{h}"

    async def get(self, model: str, text: str) -> list[float] | None:
        raw = await self.client.get(self._key(model, text))
        return json.loads(raw) if raw else None

    async def set(self, model: str, text: str, vector: list[float]):
        await self.client.setex(self._key(model, text), self.ttl, json.dumps(vector))
```

```python
# In embed_text():
async def embed_text(text: str) -> list[float]:
    cached = await embedding_cache.get(EMBEDDING_MODEL, text)
    if cached:
        return cached
    vector = await provider.embed(http, text, EMBEDDING_MODEL)
    await embedding_cache.set(EMBEDDING_MODEL, text, vector)
    return vector
```

Redis configuration (docker-compose):
```yaml
redis:
  image: redis:7-alpine
  container_name: pb-redis
  ports:
    - "6379:6379"
  volumes:
    - redis_data:/data
  command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru
  networks:
    - pb-net
  restart: unless-stopped
```

---

### OPA Policy Cache (P2-1 fix + scaling)

Two strategies, combinable:

**Strategy A: Qdrant payload filter instead of OPA loop**

Store classification at ingestion as a Qdrant payload field and use it
directly as a search filter. OPA is then called only once per request
(role check), no longer once per result.

```python
# Instead of: 50× OPA calls in the result list
# Like this: classification filter directly in Qdrant query

allowed_classifications = _get_allowed_classifications(agent_role)
# e.g. analyst → ["public", "internal"]

qdrant_filter = Filter(
    must=[
        FieldCondition(
            key="classification",
            match=MatchAny(any=allowed_classifications)
        )
    ]
)
# → one OPA call for the role, no further calls after that
```

**Strategy B: OPA decision cache (for complex policies)**

```python
# In-memory LRU or Redis
# Cache key: (agent_role, classification, action)
# TTL: 30s (policies can change)

@lru_cache(maxsize=512)
def _policy_cache_key(agent_role, classification, action):
    return f"opa:{agent_role}:{classification}:{action}"
```

---

### PgBouncer (Connection Pooling)

Prevents PostgreSQL connection exhaustion with many MCP server replicas.
PgBouncer keeps few real PG connections open, serving any number of
client connections.

```yaml
pgbouncer:
  image: bitnami/pgbouncer:latest
  container_name: pb-pgbouncer
  environment:
    POSTGRESQL_HOST: postgres
    POSTGRESQL_PORT: 5432
    POSTGRESQL_DATABASE: powerbrain
    POSTGRESQL_USERNAME: pb_admin
    POSTGRESQL_PASSWORD: ${PG_PASSWORD}
    PGBOUNCER_POOL_MODE: transaction      # transaction-level pooling
    PGBOUNCER_MAX_CLIENT_CONN: 500        # max clients
    PGBOUNCER_DEFAULT_POOL_SIZE: 20       # real PG connections
    PGBOUNCER_MIN_POOL_SIZE: 5
  ports:
    - "5433:5432"
  networks:
    - pb-net
  restart: unless-stopped
```

MCP server then connects to PgBouncer instead of directly to PG:
```env
POSTGRES_URL=postgresql://pb_admin:...@pgbouncer:5432/powerbrain
```

**Important with `transaction` mode:** `LISTEN/NOTIFY` and `SET SESSION` statements
do not work — this affects `app.current_user` in the triggers (snapshots,
history). Workaround: run versioning operations over a dedicated direct PG connection.

---

### Qdrant Cluster

Qdrant supports native cluster operation with sharding and replication:

```yaml
# Qdrant cluster node (example: 3 nodes)
qdrant-node-1:
  image: qdrant/qdrant:latest
  environment:
    QDRANT__CLUSTER__ENABLED: "true"
    QDRANT__CLUSTER__P2P__PORT: 6335
  ports:
    - "6333:6333"
  # ...

# Create collections with replication:
# PUT /collections/pb_general
# {
#   "vectors": {"size": 768, "distance": "Cosine"},
#   "shard_number": 3,       ← distributed across all nodes
#   "replication_factor": 2  ← each shard on 2 nodes
# }
```

For most setups (< 10M vectors, < 100 QPS) a single
Qdrant node is sufficient. Use cluster only when there is measurable demand.

---

### Embedding Service Pool

```yaml
# HF TEI with multiple instances (one per GPU)
tei-1:
  image: ghcr.io/huggingface/text-embeddings-inference:latest
  profiles: ["gpu"]
  command: ["--model-id", "nomic-ai/nomic-embed-text-v1", "--port", "80"]
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            device_ids: ["0"]
            capabilities: [gpu]

tei-2:
  image: ghcr.io/huggingface/text-embeddings-inference:latest
  profiles: ["gpu"]
  command: ["--model-id", "nomic-ai/nomic-embed-text-v1", "--port", "80"]
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            device_ids: ["1"]
            capabilities: [gpu]

# Nginx as load balancer in front of TEI instances:
tei-lb:
  image: nginx:alpine
  profiles: ["gpu"]
  volumes:
    - ./config/tei-nginx.conf:/etc/nginx/conf.d/default.conf
  ports:
    - "8010:80"
```

```nginx
# config/tei-nginx.conf
upstream tei_pool {
    least_conn;           # shortest queue
    server tei-1:80;
    server tei-2:80;
}
server {
    listen 80;
    location / { proxy_pass http://tei_pool; }
}
```

HF TEI supports batch embedding internally (dynamic batching) — multiple
simultaneous requests are automatically grouped together.

---

### Reranker Pool

Stateless, easily horizontal:

```yaml
reranker:
  deploy:
    replicas: 2

# Traefik labels for automatic service discovery:
labels:
  - "traefik.enable=true"
  - "traefik.http.services.reranker.loadbalancer.server.port=8082"
```

---

### Batch Embedding for the Ingestion Pipeline

During ingestion, many documents are embedded sequentially. The batch API
can be used (HF TEI + vLLM support `list[str]` input):

```python
async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embeds multiple texts in a single API call."""
    # Check if in cache
    results   = [None] * len(texts)
    uncached  = []
    indices   = []

    for i, text in enumerate(texts):
        cached = await embedding_cache.get(EMBEDDING_MODEL, text)
        if cached:
            results[i] = cached
        else:
            uncached.append(text)
            indices.append(i)

    if uncached:
        # Batch call (OpenAI format: input = list)
        resp = await http.post(f"{EMBEDDING_PROVIDER_URL}/v1/embeddings", json={
            "model": EMBEDDING_MODEL,
            "input": uncached,  # list → batch
        })
        vectors = [item["embedding"] for item in resp.json()["data"]]

        for idx, text, vector in zip(indices, uncached, vectors):
            results[idx] = vector
            await embedding_cache.set(EMBEDDING_MODEL, text, vector)

    return results
```

---

### Load Balancer: Traefik (recommended for Docker)

Traefik automatically detects containers via Docker labels, no manual
Nginx config reloading required:

```yaml
traefik:
  image: traefik:v3
  container_name: pb-traefik
  command:
    - "--providers.docker=true"
    - "--providers.docker.exposedbydefault=false"
    - "--entrypoints.web.address=:80"
    - "--entrypoints.mcp.address=:8080"
  ports:
    - "80:80"
    - "8080:8080"   # MCP endpoint (after P0-1 fix)
    - "8888:8080"   # Traefik dashboard
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
  networks:
    - pb-net

mcp-server:
  deploy:
    replicas: 3
  labels:
    - "traefik.enable=true"
    - "traefik.http.routers.mcp.rule=PathPrefix(`/`)"
    - "traefik.http.routers.mcp.entrypoints=mcp"
    - "traefik.http.services.mcp.loadbalancer.server.port=8080"
    - "traefik.http.services.mcp.loadbalancer.healthcheck.path=/health"
    # Rate limiting: max 100 req/min per agent IP
    - "traefik.http.middlewares.mcp-ratelimit.ratelimit.average=100"
    - "traefik.http.middlewares.mcp-ratelimit.ratelimit.burst=20"
```

---

## Scaling Levels

### Level 1: Single machine, few agents (current state after P0 fix)

```
1× MCP Server → Ollama (CPU) → Qdrant (single) → PostgreSQL (direct)
```

Sufficient for: development, 1–3 concurrent agents, < 50 req/min

**Fastest improvements:**
1. Embedding cache (Redis) → immediate -40% Ollama load
2. OPA fix (P2-1, Qdrant filter) → immediate -1–2s per search request

---

### Level 2: Single machine with GPU, moderate load

```
2× MCP Server → HF TEI (GPU) → Qdrant (single) → PgBouncer → PostgreSQL
             → vLLM (GPU, optional)
             → 2× Reranker
Traefik as load balancer
Redis for embedding cache
```

Sufficient for: 5–20 concurrent agents, < 500 req/min

---

### Level 3: Multi-node, high load

```
Traefik Cluster
    ↓
N× MCP Server (different nodes)
    → TEI/vLLM Pool (GPU nodes)
    → Qdrant Cluster (3 nodes, sharding + replication)
    → PgBouncer → PG Primary + 1× Read Replica
    → OPA Pool (2–3 instances)
    → Reranker Pool (GPU or CPU)
Redis Cluster for embedding cache
```

Sufficient for: 50+ concurrent agents, > 1000 req/min

---

### Level 4: Kubernetes

From level 3 onwards, Kubernetes becomes worthwhile: HPA (Horizontal Pod Autoscaler)
scales MCP server and reranker automatically based on CPU/request rate. Qdrant and
PostgreSQL run as StatefulSets.

```yaml
# Kubernetes HPA for MCP Server
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: mcp-server-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: mcp-server
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Pods
      pods:
        metric:
          name: pb_mcp_requests_total
        target:
          type: AverageValue
          averageValue: "50"   # max 50 req/s per pod
```

---

## Ingestion Under Load: Queue-Based Design

Currently: ingestion is synchronous (request → wait → response).
With many parallel ingestions this blocks the HTTP thread.

**Recommendation: Async job queue (Redis Streams or Postgres LISTEN/NOTIFY)**

```
POST /ingest → Job in queue → 202 Accepted + job_id
                    ↓
             Worker Pool (3× ingestion worker)
                    ↓
             embed_batch() → Qdrant upsert
                    ↓
             Job status in PG → GET /ingest/status/{job_id}
```

```python
# Simplest implementation with PostgreSQL (no extra queue service):
CREATE TABLE ingestion_jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status      VARCHAR(20) DEFAULT 'queued',  -- queued, processing, done, failed
    source      TEXT NOT NULL,
    source_type VARCHAR(50),
    created_at  TIMESTAMPTZ DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error       TEXT,
    result      JSONB
);
-- Worker polls: SELECT ... WHERE status='queued' FOR UPDATE SKIP LOCKED
```

`FOR UPDATE SKIP LOCKED` is the PostgreSQL-native mechanism for
worker-safe job queues without Redis/RabbitMQ.

---

## Summary: Priorities

| Measure | When | Effort | Impact |
|---------|------|--------|--------|
| P0-1 fix (HTTP transport) | Immediately | Medium | Prerequisite for everything |
| OPA fix (Qdrant filter, P2-1) | Immediately | Small | -1–2s per search request |
| Embedding cache (Redis) | Early | Small | -40–70% embedding load |
| PgBouncer | Early | Small | Connection stability |
| Batch embedding in ingestion | Early | Small | 5–10× ingestion throughput |
| MCP server horizontal (2–3×) + Traefik | Level 2 | Medium | Parallelism |
| HF TEI / vLLM instead of Ollama | Level 2 | Medium | Embedding throughput |
| Reranker pool | Level 2 | Small | Reranking parallelism |
| Ingestion job queue | Level 2 | Medium | Decoupling, backpressure |
| Qdrant cluster | Level 3 | Large | Only needed above > 5M vectors |
| PG read replica | Level 3 | Medium | Analytics / eval queries |
| Kubernetes + HPA | Level 4 | Large | Autoscaling |
