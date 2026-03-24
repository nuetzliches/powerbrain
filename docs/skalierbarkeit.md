# Skalierbarkeit & Load Balancing

Architekturbetrachtung für Multi-Agent-Szenarien mit hoher paralleler Last.

---

## Aktuelle Bottlenecks (Ist-Analyse)

Bevor Horizontal Scaling hilft, müssen die per-Request-Bottlenecks behoben sein.
Sonst skaliert man nur das Problem.

### Per-Request Bottlenecks (müssen zuerst behoben werden)

| Problem | Auswirkung | Fix |
|---------|-----------|-----|
| **50 serielle OPA-Calls** pro Suchanfrage (P2-1) | 1–2,5s Overhead allein für Policy-Checks | OPA-Batch oder Qdrant-Filter |
| **Embedding sequenziell** (Ollama, ein Request nach dem anderen) | Bei 10 parallelen Agents warten alle aufeinander | Embedding-Cache + Batch-API |
| **asyncpg Pool zu klein** (min=2, max=10) | Connection-Erschöpfung ab ~5 parallelen Agents | Pool-Größe + PgBouncer |
| **MCP-Transport stdio** (P0-1) | Kein Netzwerkzugriff möglich | HTTP-Transport (Voraussetzung für alles) |

### Infrastruktur-Bottlenecks (werden durch Horizontal Scaling adressiert)

| Komponente | Bottleneck-Typ | Strategie |
|-----------|---------------|-----------|
| MCP-Server | CPU/IO-gebunden | Horizontal (stateless) |
| Ollama (Embeddings) | GPU/CPU-gebunden, sequenziell | Batch-API + Cache + mehrere Instanzen |
| vLLM / HF TEI | GPU-gebunden, intern gebaztcht | Mehrere Instanzen |
| Reranker | CPU/GPU-gebunden | Horizontal (stateless) |
| Qdrant | IO/Memory-gebunden | Nativer Cluster + Sharding |
| PostgreSQL | IO-gebunden | PgBouncer + Read Replica |
| OPA | CPU, in-memory | Horizontal trivial |

---

## Ziel-Architektur (Multi-Agent, hohe Last)

```
                     Agents (n × parallel)
                           │
                    ┌──────┴──────┐
                    │  Load       │
                    │  Balancer   │  (Traefik / Nginx / Envoy)
                    │  + Rate     │  Rate-Limit pro Agent-ID
                    │  Limiting   │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    MCP-Server 1     MCP-Server 2     MCP-Server N    (stateless, skalierbar)
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
└──────┬──────┘   │   Replikation)  │   └─────────────────┘
       │          └─────────────────┘
       ▼
┌──────────────────────────────────┐
│  Embedding Service Pool          │
│  ├─ HF TEI 1 (GPU 0)             │
│  ├─ HF TEI 2 (GPU 1)             │  ← oder vLLM multi-GPU
│  └─ Ollama (CPU Fallback)        │
└──────────────────────────────────┘

        ┌────────────────────┐
        │  Reranker Pool     │
        │  ├─ Reranker 1     │
        │  └─ Reranker 2     │
        └────────────────────┘

        ┌────────────────────┐
        │  OPA Pool          │
        │  ├─ OPA 1          │  ← alle teilen dasselbe Bundle
        │  └─ OPA 2          │
        └────────────────────┘
```

---

## Komponenten-Strategie im Detail

### MCP-Server (nach P0-1-Fix)

Vollständig stateless — kein lokaler Zustand, alles in PG/Qdrant.
Einfach horizontal skalierbar.

```yaml
# docker-compose.yml (mit Replicas)
mcp-server:
  deploy:
    replicas: 3
    resources:
      limits:
        cpus: '1.0'
        memory: 512M

# ODER: Kubernetes HPA
# autoscaling: min=2, max=10, cpu-threshold=60%
```

**Prometheus-Metriken bei mehreren Instanzen:**
Jede Instanz exponiert `/metrics` auf ihrem eigenen Port. Prometheus scrapt
alle Instanzen, unterscheidet über `instance`-Label. Kein Pushgateway nötig.

**Wichtig:** asyncpg-Pool-Größe an Replica-Anzahl anpassen:
```
max_connections (PG) = replicas × pool_max + overhead
# 3 Replicas × 10 = 30 → PG max_connections: 50 reicht
# Mit PgBouncer: keine direkte Relation mehr (s.u.)
```

---

### Embedding-Cache (höchster ROI)

Gleiche Query → identischer Vektor. Cache-Hit-Rate in der Praxis: 40–70%
(ähnliche Anfragen von Agents, wiederholte Lookups).

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

Redis-Konfiguration (docker-compose):
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

### OPA-Policy-Cache (P2-1-Fix + Skalierung)

Zwei Strategien, kombinierbar:

**Strategie A: Qdrant-Payload-Filter statt OPA-Loop**

Klassifizierung bei Ingestion als Qdrant-Payload-Feld speichern und direkt
als Suchfilter nutzen. OPA wird nur noch einmalig pro Request aufgerufen
(Rollen-Prüfung), nicht mehr pro Ergebnis.

```python
# Statt: 50× OPA-Call in der Ergebnisliste
# So: Klassifizierungsfilter direkt in Qdrant-Query

allowed_classifications = _get_allowed_classifications(agent_role)
# z.B. analyst → ["public", "internal"]

qdrant_filter = Filter(
    must=[
        FieldCondition(
            key="classification",
            match=MatchAny(any=allowed_classifications)
        )
    ]
)
# → ein OPA-Call für die Rolle, danach kein weiterer
```

**Strategie B: OPA-Entscheidungs-Cache (für komplexe Policies)**

```python
# In-Memory LRU oder Redis
# Cache-Key: (agent_role, classification, action)
# TTL: 30s (Policies können sich ändern)

@lru_cache(maxsize=512)
def _policy_cache_key(agent_role, classification, action):
    return f"opa:{agent_role}:{classification}:{action}"
```

---

### PgBouncer (Connection Pooling)

Verhindert PostgreSQL-Connection-Erschöpfung bei vielen MCP-Server-Replicas.
PgBouncer hält wenige echte PG-Verbindungen offen, bedient beliebig viele
Client-Verbindungen.

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
    PGBOUNCER_MAX_CLIENT_CONN: 500        # max Clients
    PGBOUNCER_DEFAULT_POOL_SIZE: 20       # echte PG-Verbindungen
    PGBOUNCER_MIN_POOL_SIZE: 5
  ports:
    - "5433:5432"
  networks:
    - pb-net
  restart: unless-stopped
```

MCP-Server verbindet sich dann gegen PgBouncer statt direkt PG:
```env
POSTGRES_URL=postgresql://pb_admin:...@pgbouncer:5432/powerbrain
```

**Wichtig bei `transaction`-Mode:** `LISTEN/NOTIFY` und `SET SESSION`-Statements
funktionieren nicht — betrifft `app.current_user` in den Triggern (Snapshots,
History). Workaround: Versioning-Operationen über dedizierte direkte PG-Verbindung.

---

### Qdrant-Cluster

Qdrant unterstützt nativen Cluster-Betrieb mit Sharding und Replikation:

```yaml
# Qdrant Cluster-Node (Beispiel: 3 Nodes)
qdrant-node-1:
  image: qdrant/qdrant:latest
  environment:
    QDRANT__CLUSTER__ENABLED: "true"
    QDRANT__CLUSTER__P2P__PORT: 6335
  ports:
    - "6333:6333"
  # ...

# Collections mit Replikation erstellen:
# PUT /collections/pb_general
# {
#   "vectors": {"size": 768, "distance": "Cosine"},
#   "shard_number": 3,       ← über alle Nodes verteilt
#   "replication_factor": 2  ← jeder Shard auf 2 Nodes
# }
```

Für die meisten Setups (< 10M Vektoren, < 100 QPS) reicht ein einzelner
Qdrant-Node problemlos. Cluster erst ab messbarem Bedarf.

---

### Embedding Service Pool

```yaml
# HF TEI mit mehreren Instanzen (eine pro GPU)
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

# Nginx als Load Balancer vor TEI-Instanzen:
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
    least_conn;           # kürzeste Warteschlange
    server tei-1:80;
    server tei-2:80;
}
server {
    listen 80;
    location / { proxy_pass http://tei_pool; }
}
```

HF TEI unterstützt batch-Embedding intern (dynamic batching) — mehrere
simultane Requests werden automatisch zusammengefasst.

---

### Reranker-Pool

Stateless, einfach horizontal:

```yaml
reranker:
  deploy:
    replicas: 2

# Traefik-Labels für automatische Service-Discovery:
labels:
  - "traefik.enable=true"
  - "traefik.http.services.reranker.loadbalancer.server.port=8082"
```

---

### Batch-Embedding für die Ingestion-Pipeline

Bei der Ingestion werden viele Dokumente sequenziell embedded. Batch-API
nutzbar (HF TEI + vLLM unterstützen `list[str]` Input):

```python
async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embeddet mehrere Texte in einem einzigen API-Call."""
    # Prüfen ob im Cache
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
        # Batch-Call (OpenAI-Format: input = list)
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

### Load Balancer: Traefik (empfohlen für Docker)

Traefik erkennt Container automatisch über Docker-Labels, kein manuelles
Nginx-Config-Reloading nötig:

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
    - "8080:8080"   # MCP-Endpunkt (nach P0-1-Fix)
    - "8888:8080"   # Traefik Dashboard
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
    # Rate-Limiting: max 100 req/min pro Agent-IP
    - "traefik.http.middlewares.mcp-ratelimit.ratelimit.average=100"
    - "traefik.http.middlewares.mcp-ratelimit.ratelimit.burst=20"
```

---

## Skalierungsstufen

### Stufe 1: Einzelmaschine, wenige Agents (aktueller Stand nach P0-Fix)

```
1× MCP-Server → Ollama (CPU) → Qdrant (single) → PostgreSQL (direct)
```

Reicht für: Entwicklung, 1–3 gleichzeitige Agents, < 50 req/min

**Schnellste Verbesserungen:**
1. Embedding-Cache (Redis) → sofort -40% Ollama-Last
2. OPA-Fix (P2-1, Qdrant-Filter) → sofort -1–2s pro Suchanfrage

---

### Stufe 2: Einzelmaschine mit GPU, moderate Last

```
2× MCP-Server → HF TEI (GPU) → Qdrant (single) → PgBouncer → PostgreSQL
             → vLLM (GPU, optional)
             → 2× Reranker
Traefik als Load Balancer
Redis für Embedding-Cache
```

Reicht für: 5–20 gleichzeitige Agents, < 500 req/min

---

### Stufe 3: Multi-Node, hohe Last

```
Traefik Cluster
    ↓
N× MCP-Server (verschiedene Nodes)
    → TEI/vLLM Pool (GPU-Nodes)
    → Qdrant Cluster (3 Nodes, Sharding + Replikation)
    → PgBouncer → PG Primary + 1× Read Replica
    → OPA Pool (2–3 Instanzen)
    → Reranker Pool (GPU oder CPU)
Redis Cluster für Embedding-Cache
```

Reicht für: 50+ gleichzeitige Agents, > 1000 req/min

---

### Stufe 4: Kubernetes

Ab Stufe 3 wird Kubernetes sinnvoll: HPA (Horizontal Pod Autoscaler) skaliert
MCP-Server und Reranker automatisch nach CPU/Request-Rate. Qdrant und PostgreSQL
laufen als StatefulSets.

```yaml
# Kubernetes HPA für MCP-Server
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
          averageValue: "50"   # max 50 req/s pro Pod
```

---

## Ingestion unter Last: Queue-basiertes Design

Aktuell: Ingestion ist synchron (Request → warte → Response).
Bei vielen parallelen Ingestions blockiert das den HTTP-Thread.

**Empfehlung: Async Job Queue (Redis Streams oder Postgres LISTEN/NOTIFY)**

```
POST /ingest → Job in Queue → 202 Accepted + job_id
                    ↓
             Worker Pool (3× Ingestion Worker)
                    ↓
             embed_batch() → Qdrant upsert
                    ↓
             Job Status in PG → GET /ingest/status/{job_id}
```

```python
# Einfachste Implementierung mit PostgreSQL (kein extra Queue-Service):
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
-- Worker pollt: SELECT ... WHERE status='queued' FOR UPDATE SKIP LOCKED
```

`FOR UPDATE SKIP LOCKED` ist der PostgreSQL-native Mechanismus für
worker-safe Job-Queues ohne Redis/RabbitMQ.

---

## Zusammenfassung: Prioritäten

| Maßnahme | Wann | Aufwand | Wirkung |
|----------|------|---------|---------|
| P0-1 Fix (HTTP Transport) | Sofort | Mittel | Voraussetzung für alles |
| OPA-Fix (Qdrant-Filter, P2-1) | Sofort | Klein | -1–2s pro Suchanfrage |
| Embedding-Cache (Redis) | Früh | Klein | -40–70% Embedding-Last |
| PgBouncer | Früh | Klein | Connection-Stabilität |
| Batch-Embedding in Ingestion | Früh | Klein | 5–10× Ingestion-Throughput |
| MCP-Server horizontal (2–3×) + Traefik | Stufe 2 | Mittel | Parallelität |
| HF TEI / vLLM statt Ollama | Stufe 2 | Mittel | Embedding-Throughput |
| Reranker Pool | Stufe 2 | Klein | Reranking-Parallelität |
| Ingestion Job Queue | Stufe 2 | Mittel | Entkopplung, Backpressure |
| Qdrant Cluster | Stufe 3 | Groß | Nur bei > 5M Vektoren nötig |
| PG Read Replica | Stufe 3 | Mittel | Analytics / Eval-Queries |
| Kubernetes + HPA | Stufe 4 | Groß | Autoscaling |
