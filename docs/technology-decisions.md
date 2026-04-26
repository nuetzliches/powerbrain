# Technology Decisions & Extension Options

Discussed architectural decisions and their rationale.

---

## T-1: Vision Language Models (VLM) alongside Ollama

### Question

Can we connect a local VLM (Vision Language Model) in addition to Ollama
for multimodal content (images, diagrams, PDFs with graphics)?

### Answer: Yes — Ollama itself supports VLMs

Ollama can natively host vision models. No separate service needed:

| Model             | Strength                     | RAM requirement |
| ----------------- | ---------------------------- | --------------- |
| `llava:7b`        | General purpose, good balance | ~6 GB          |
| `llama3.2-vision` | Text recognition in images   | ~6 GB           |
| `moondream2`      | Very fast, compact           | ~2 GB           |
| `llava:34b`       | High quality, slow           | ~25 GB          |

### Conceptual distinction (important)

```
Image/Diagram
    ↓
VLM (llava)          → generates text description: "Architecture diagram with 3 services..."
    ↓
Embedding (nomic-embed-text)  → Vector [0.12, -0.34, ...]
    ↓
Qdrant
```

**VLM ≠ multimodal embedding.** The VLM _describes_ the image in text,
which is then embedded normally. You search semantically over the
description, not directly over pixels.

Alternative: CLIP-based image embeddings (image directly → vector). Advantage:
faster. Disadvantage: separate embedding service, different vector dimension,
separate Qdrant collection. Not recommended for this project.

### Implementation

**Ingestion pipeline** (new VLM module):

```python
async def describe_image(image_bytes: bytes, mime_type: str) -> str:
    """Sends image to Ollama VLM, receives text description in return."""
    resp = await http.post(f"{OLLAMA_URL}/api/generate", json={
        "model": VLM_MODEL,  # env: VLM_MODEL=llava:7b
        "prompt": "Describe this image precisely in English...",
        "images": [base64.b64encode(image_bytes).decode()],
        "stream": False,
    })
    return resp.json()["response"]
```

Integrated in the adapter layer (→ T-4) when processing:

- PDF pages with graphics (via `pymupdf`)
- Standalone image files
- DOCX/PPTX with embedded diagrams

**Configuration** (`.env`):

```
VLM_ENABLED=false          # default off, opt-in
VLM_MODEL=llava:7b
VLM_MAX_IMAGE_SIZE_MB=10
```

### Ollama container adjustment

No additional container. Model needs to be pulled once:

```bash
docker exec pb-ollama ollama pull llava:7b
```

---

## T-2: Git Server Support (Forgejo as an example)

### Question

Is Forgejo a fixed requirement, or do all common Git-based servers work?

### Answer: Forgejo is an example — all common Git servers work

The project has two integration points with Git servers:

#### Integration point 1: OPA bundle polling

OPA polls policies as an HTTP bundle. This is purely URL-based — any
server that serves a `.tar.gz` over HTTP works:

```yaml
# OPA config — server-agnostic:
services:
  git-server:
    url: ${GIT_SERVER_URL}
    credentials:
      bearer:
        token: ${GIT_TOKEN}
bundles:
  pb:
    service: git-server
    resource: /api/v1/repos/org/pb-policies/raw/bundle.tar.gz # adjust path
    polling:
      min_delay_seconds: 10
```

| Server          | Bundle URL schema                                                             |
| --------------- | ----------------------------------------------------------------------------- |
| Forgejo/Gitea   | `/api/v1/repos/{org}/{repo}/raw/{file}`                                       |
| GitHub          | `/raw/{org}/{repo}/{branch}/{file}` (via API or raw.githubusercontent.com)    |
| GitLab          | `/api/v4/projects/{id}/repository/files/{file}/raw`                           |
| Bitbucket Cloud | `/2.0/repositories/{ws}/{repo}/src/{branch}/{file}`                           |

#### Integration point 2: Code ingestion (reading repo contents)

Currently uses Forgejo API paths. With the adapter layer (→ T-4) this is abstracted.

### Recommended configuration

```env
# Instead of FORGEJO_URL/FORGEJO_TOKEN:
GIT_SERVER_TYPE=forgejo   # forgejo | github | gitlab | bitbucket
GIT_SERVER_URL=https://git.intern.example.com
GIT_TOKEN=...
GIT_ORG=pb-org
```

The Git adapter (→ T-4) translates to the respective API dialect.

---

## T-3: Monitoring — External OTel Collector, optional Grafana stack

### Question

Can monitoring be offered via an external OpenTelemetry Collector,
making the local Grafana stack optional?

### Answer: Yes — this is actually the recommended architecture

The **OpenTelemetry Collector** is the standardized routing layer between
services and observability backends:

```
Services (MCP, Reranker, Ingestion)
    │ OTLP (gRPC :4317)
    ▼
┌─────────────────────────────┐
│  OTel Collector             │
│  ├─ Receiver: OTLP          │
│  ├─ Processor: batch, attrs │
│  └─ Exporters:              │
│     ├─ Tempo (local)        │  ← optional
│     ├─ Grafana Cloud        │  ← or external
│     ├─ Datadog              │  ← or external
│     └─ Jaeger               │  ← or external
└─────────────────────────────┘
```

Services only know `OTLP_ENDPOINT` — they are backend-agnostic.

### Docker Compose profiles

```yaml
# docker-compose.yml
services:
  otel-collector:          # always active (lightweight)
    image: otel/opentelemetry-collector-contrib:latest
    profiles: []           # no profile = always started
    ...

  prometheus:              # optional
    profiles: ["monitoring-local"]
    ...

  grafana:                 # optional
    profiles: ["monitoring-local"]
    ...

  tempo:                   # optional
    profiles: ["monitoring-local"]
    ...
```

**Operation with local stack:**

```bash
docker compose --profile monitoring-local up -d
```

**Operation with external backend (e.g. Grafana Cloud):**

```bash
# .env:
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic ...
docker compose up -d   # only otel-collector, no local stack
```

### OTel Collector configuration (`monitoring/otel-collector.yml`)

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  batch:
    timeout: 1s
  resource:
    attributes:
      - key: service.namespace
        value: pb
        action: upsert

exporters:
  # Local Tempo (when monitoring-local profile is active)
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true

  # Optional external backend
  otlp/external:
    endpoint: ${OTEL_EXTERNAL_ENDPOINT:-}
    headers:
      authorization: ${OTEL_EXTERNAL_AUTH:-}

  # Prometheus-compatible metrics (for scraping)
  prometheus:
    endpoint: "0.0.0.0:8889"

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch, resource]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus]
```

### Prometheus metrics: scraping vs. push

Currently: Prometheus actively _scrapes_ services (`/metrics` endpoint).
Alternative: Services _push_ metrics via OTLP to the collector.

|                      | Scraping (current)               | OTLP Push                        |
| -------------------- | -------------------------------- | -------------------------------- |
| Prometheus required  | Yes                              | No (collector is sufficient)     |
| Pull model           | Yes                              | No                               |
| Firewall-friendly    | Less                             | Yes (collector outbound)         |
| Recommendation       | Simpler for local operation      | Better for external backends     |

**Recommendation:** Offer both in parallel — scraping remains as fallback,
OTLP push for external backends. Services don't need to know both methods:
The collector is the only endpoint.

---

## T-4: Adapter layer for additional data sources

### Question

Should an adapter layer for additional data sources be implemented?
Where should it be positioned — before or after the privacy layer?

### Answer: Yes — mandatory before the privacy layer

### Rationale: Why BEFORE the PII scanner

```
External source (PDF, Git, XLSX, API, ...)
    ↓
[ADAPTER LAYER]            ← Normalization: Binary → Text + Metadata
    ↓
[PII Scanner (Presidio)]   ← first layer that sees text
    ↓
[OPA Policy]               ← Classification + purpose check
    ↓
[Embedding (Ollama)]
    ↓
[Qdrant]
```

The PII scanner can only process text. If PDFs/DOCX were passed directly into
the privacy layer, document parsing would have to occur there —
which violates the single-responsibility principle. The adapter layer always
delivers `NormalizedDocument`, regardless of the source.

### Common abstraction

```python
# ingestion/adapters/base.py

from dataclasses import dataclass, field
from typing import AsyncIterator

@dataclass
class NormalizedDocument:
    content: str                    # Extracted full text
    content_type: str               # "text", "code", "table", "image_description"
    source_ref: str                 # Original URI/path
    source_type: str                # "git", "pdf", "xlsx", "api", ...
    language: str | None = None     # "de", "en", None = unknown
    metadata: dict = field(default_factory=dict)
    # Contains: title, author, created_at, repo, path, etc.
    chunks: list[str] | None = None  # Optional: pre-cut chunks

class SourceAdapter:
    """Abstract base for all data source adapters."""

    async def fetch(self) -> AsyncIterator[NormalizedDocument]:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError
```

### Planned adapters

| Adapter             | Sources                                   | Priority |
| ------------------- | ----------------------------------------- | -------- |
| `GitAdapter`        | Forgejo, GitHub, GitLab, Bitbucket, Gitea | High     |
| `FileAdapter`       | PDF, DOCX, XLSX, Markdown, TXT            | High     |
| `DatabaseAdapter`   | PostgreSQL dump, CSV, JSON                | Medium   |
| `ConfluenceAdapter` | Confluence REST API                       | Medium   |
| `WebAdapter`        | HTTP/HTML scraping, sitemaps              | Low      |
| `KafkaAdapter`      | Streaming content                         | Low      |
| `EmailAdapter`      | IMAP, EML files                           | Low      |

### Git adapter (also resolves T-2)

```python
# ingestion/adapters/git_adapter.py

class GitAdapter(SourceAdapter):
    """Supports Forgejo, Gitea, GitHub, GitLab, Bitbucket."""

    PROVIDERS = {
        "forgejo": ForgejoProvider,
        "gitea":   ForgejoProvider,   # API-compatible
        "github":  GitHubProvider,
        "gitlab":  GitLabProvider,
        "bitbucket": BitbucketProvider,
    }

    def __init__(self, server_type: str, url: str, token: str,
                 org: str, repo: str, branch: str = "main"):
        self.provider = self.PROVIDERS[server_type](url, token)
        ...

    async def fetch(self) -> AsyncIterator[NormalizedDocument]:
        async for file in self.provider.list_files(self.org, self.repo):
            content = await self.provider.get_file_content(file.path)
            yield NormalizedDocument(
                content=content,
                content_type=_detect_content_type(file.path),
                source_ref=f"{self.url}/{self.org}/{self.repo}/blob/{file.sha}",
                source_type="git",
                language=_detect_language(file.path),
                metadata={"repo": self.repo, "path": file.path, "sha": file.sha},
            )
```

### Directory structure

```
ingestion/
├── ingestion_api.py         ← FastAPI app (to be implemented, P0-2)
├── adapters/
│   ├── base.py              ← NormalizedDocument, SourceAdapter
│   ├── git_adapter.py       ← Git server (all providers)
│   ├── file_adapter.py      ← PDF/DOCX/XLSX via pymupdf/python-docx
│   ├── database_adapter.py  ← CSV/JSON/PG dumps
│   └── providers/
│       ├── forgejo.py
│       ├── github.py
│       ├── gitlab.py
│       └── bitbucket.py
├── pii_scanner.py           ← unchanged
├── retention_cleanup.py     ← unchanged
└── snapshot_service.py      ← unchanged
```

### Interaction with VLM (T-1)

The `FileAdapter` automatically calls the VLM for image content:

```python
# In file_adapter.py:
if page.has_images and VLM_ENABLED:
    for image in page.images:
        description = await vlm.describe_image(image.bytes)
        # description is added to the chunk text
        chunk_text += f"\n[Figure: {description}]"
```

The VLM is thus transparently integrated into the ingestion pipeline —
no separate MCP tool call needed.

---

---

## T-5: vLLM as an alternative to Ollama

### Question

Can vLLM replace Ollama, and when does that make sense?

### Comparison

|                       | Ollama                                  | vLLM                                        |
| --------------------- | --------------------------------------- | ------------------------------------------- |
| **Primary purpose**   | Developer experience, local use         | Production-grade LLM serving                |
| **Throughput**        | Sequential, low                         | Continuous batching → 10–50× higher         |
| **GPU memory**        | Standard allocation                     | PagedAttention → significantly more efficient |
| **Parallelism**       | Poor (one request after another)        | Very good (dozens simultaneous)             |
| **CPU-only**          | ✅ Good                                 | ⚠️ Experimental, very slow                 |
| **API**               | Ollama-specific + OpenAI-compat.        | OpenAI-compatible (native)                  |
| **VLM support**       | LLaVA, moondream2, llama3.2-vision      | LLaVA, InternVL, Qwen-VL — better batched  |
| **Embedding models**  | Broad (nomic, mxbai, all-minilm ...)    | Limited                                     |
| **Multi-GPU**         | No                                      | Tensor parallelism, yes                     |
| **Setup**             | `ollama pull model`                     | CUDA + Docker + model configuration         |

### The problem with a direct replacement

vLLM is **not a full embedding service**. It supports `/v1/embeddings`,
but the embedding model selection is narrower than with Ollama. `nomic-embed-text`
is not guaranteed to be available. For high-performance embeddings there are better
dedicated alternatives:

| Service                                         | Strength                                           |
| ----------------------------------------------- | -------------------------------------------------- |
| **HuggingFace Text Embeddings Inference (TEI)** | Many models, very fast, OpenAI-compat.             |
| **infinity-embedding**                          | Lightweight, OpenAI-compat., simple setup          |
| **Ollama**                                      | Simple, broad model support, CPU-capable           |

### Recommended split

```
Embeddings:  infinity (Prod/GPU)
LLM/VLM:     vLLM (Prod/GPU)
```

Both roles configurable via separate endpoints:

```env
EMBEDDING_PROVIDER_URL=http://infinity:80
EMBEDDING_MODEL=nomic-embed-text

LLM_PROVIDER_URL=http://vllm:8000
LLM_MODEL=llama3.2-vision                   # for VLM
```

### Provider abstraction (LLMProvider interface)

Since Ollama (OpenAI-compatible mode), vLLM, HF TEI, and external
services all offer an OpenAI-compatible API, a thin abstraction suffices:

```python
# mcp-server/llm_provider.py  and  ingestion/llm_provider.py

import httpx

class LLMProvider:
    """
    Thin abstraction over OpenAI-compatible LLM/embedding endpoints.
    Supports: Ollama, vLLM, HF TEI, infinity, OpenAI (with DPA!).
    """

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.headers  = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def embed(self, http: httpx.AsyncClient, text: str, model: str) -> list[float]:
        resp = await http.post(
            f"{self.base_url}/v1/embeddings",
            headers=self.headers,
            json={"model": model, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    async def generate(self, http: httpx.AsyncClient, prompt: str,
                       model: str, images: list[str] | None = None) -> str:
        """Text generation, optionally with images (VLM)."""
        messages = [{"role": "user", "content": prompt}]
        if images:
            # OpenAI Vision format
            messages = [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                *[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}
                  for img in images],
            ]}]

        resp = await http.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self.headers,
            json={"model": model, "messages": messages, "stream": False},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
```

**Ollama compatibility:** Ollama has exposed an OpenAI-compatible
API under `/v1/` since v0.1.24 — the provider code works with it without changes.

### When to choose vLLM

- More than 3–5 concurrent agents (parallelism bottleneck with Ollama)
- GPU available (vLLM is optimized for CUDA, CPU is very slow)
- VLM under load (multiple concurrent image descriptions in ingestion batch)
- Models > 13B parameters (PagedAttention saves critical GPU RAM)

### Docker Compose extension

```yaml
# ── vLLM (optional, replaces Ollama for LLM/VLM) ──────────
vllm:
  image: vllm/vllm-openai:latest
  container_name: pb-vllm
  profiles: ["gpu"]
  ports:
    - "8000:8000"
  volumes:
    - vllm_models:/root/.cache/huggingface
  environment:
    HUGGING_FACE_HUB_TOKEN: ${HF_TOKEN:-}
  command:
    - "--model"
    - "${VLLM_MODEL:-llava-hf/llava-1.5-7b-hf}"
    - "--dtype"
    - "bfloat16"
    - "--max-model-len"
    - "4096"
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
  networks:
    - pb-net
  restart: unless-stopped

# ── HF Text Embeddings Inference (optional) ──────────────
tei:
  image: ghcr.io/huggingface/text-embeddings-inference:latest
  container_name: pb-tei
  profiles: ["gpu"]
  ports:
    - "8010:80"
  volumes:
    - tei_models:/data
  command:
    - "--model-id"
    - "nomic-ai/nomic-embed-text-v1"
    - "--port"
    - "80"
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
  networks:
    - pb-net
  restart: unless-stopped
```

**Operation with GPU stack:**

```bash
docker compose --profile gpu up -d
# .env:
# EMBEDDING_PROVIDER_URL=http://tei:80
# LLM_PROVIDER_URL=http://vllm:8000
# LLM_MODEL=llava-hf/llava-1.5-7b-hf
```

---

## T-6: Document extraction — markitdown vs. Docling (B-52)

### Question

Microsoft [markitdown](https://github.com/microsoft/markitdown) is the
default extractor in `ingestion/content_extraction/`. IBM's
[Docling](https://github.com/DS4SD/docling) (MIT, 2024+) advertises
better table/layout understanding and explicit RAG-readiness. Should
we **stay with markitdown**, **switch to Docling**, or **run Docling as
an opt-in backend alongside**?

### Decision framework

The choice is data-driven, not philosophical. Three observable axes:

| Axis | Markitdown | Docling | Why it matters |
|---|---|---|---|
| **Plain Office handbooks** (DOCX, PPTX, XLSX) | Strong, used in production today. Fallbacks (`python-docx`, `openpyxl`, `python-pptx`) cover edge cases. | Comparable on simple docs; outperforms on heavily formatted XLSX with merged cells / multi-sheet refs. | Most enterprise corpora. |
| **Scanned / image-heavy PDFs** | OCR via optional Tesseract fallback (`OCR_FALLBACK_ENABLED`). Layout reflows poorly. | Built-in TableFormer + DocLayNet models — recovers tables and reading order. | Pharma SOPs, insurance, legal. Where the difference is largest. |
| **Tabular HTML / rich PDFs with tables** | Good for HTML, weak for PDF tables (linearises). | Reconstructs tables into structured Markdown / HTML rows. | Financial reports, research papers. |

Cost axes that pull the other way:

| Axis | Markitdown | Docling | Notes |
|---|---|---|---|
| **Latency (CPU)** | ~10–200 ms / doc (fast path) | ~1–10 s / doc (model inference) | Docling is order-of-magnitude slower without GPU. |
| **Container size** | ~50 MB | ~700 MB (PyTorch + DocLayNet + TableFormer) | Affects pull time, not ongoing footprint. |
| **GPU benefit** | None (CPU work) | 5–10× speedup with CUDA | Lifts Docling into "fast enough" only on GPU hosts. |
| **Maintenance surface** | One Python lib, MIT, Microsoft. | Multiple ML model artifacts, MIT, IBM. | Both well-maintained; Docling has more moving parts. |

### Decision: **stay with markitdown by default; ship Docling as an opt-in second backend later**

The current production corpus (German Office handbooks, Confluence
exports, GitHub Markdown) is markitdown's sweet spot. Docling's
advantage materialises only when the corpus shifts toward scanned
PDFs and tabular reports. Until that shift is *measured* we don't
spend the +650 MB image / +1 s latency every existing user pays.

The opt-in path matches our existing `ContentExtractor` shape: a new
`DOCUMENT_EXTRACTOR_BACKEND` env variable can route to either backend,
and Docling can land alongside markitdown in
`ingestion/content_extraction/` without touching the public
`extract()` signature. This mirrors the `RERANKER_BACKEND` and
`LLM_PROVIDER_URL` patterns that already exist.

### When to revisit (triggers)

- A pilot customer hits >20% of their corpus on scanned PDFs with
  tables, and `OCR_FALLBACK_ENABLED=true` produces unreadable output.
- A repeat support ticket on `pb_extract_requests_total{status="error"}`
  tied to PDFs/XLSX with merged cells.
- A second adapter (legal, insurance, healthcare) lands where layout
  fidelity is part of the value proposition.

Any of those should rerun the benchmark in
`scripts/benchmark_extractors.py` (see below) on the customer's actual
documents, then drive the implementation ticket.

### Benchmark harness

`scripts/benchmark_extractors.py` is the harness that produces the
data this ADR is missing today. It runs both extractors against
`testdata/documents/` (or any directory the user passes), and prints
per-file output size, character count, and wall-clock latency for each
extractor. The script self-installs neither Docling nor markitdown —
operators run it inside an environment where both are available
(typically the ingestion image with `pip install docling` added
ad-hoc, or a temporary venv).

```bash
# In a Python 3.12+ venv with both installed:
pip install markitdown docling
python3 scripts/benchmark_extractors.py testdata/documents/
```

The harness deliberately stays out of the production codepath — there
is no Docker image, no compose service, no runtime dependency. It
exists purely to support the next iteration of this ADR.

### Out of scope

- Implementation of the second backend. This ticket (B-52) is
  research only; the implementation lands in a separate ticket once
  benchmark data justifies it.
- A "smart" extractor that picks markitdown vs. Docling per document.
  Premature; the benchmark will tell us whether one strictly dominates
  on the corpus that matters.

---

## Summary of recommendations

| Topic           | Dev (CPU)                                | Prod (GPU)              | Effort                         |
| --------------- | ---------------------------------------- | ----------------------- | ------------------------------ |
| Embeddings      | Ollama                                   | HF TEI or infinity      | Small (provider abstraction)   |
| LLM/VLM         | Ollama                                   | vLLM                    | Medium (Docker profile)        |
| VLM integration | Ollama `llava:7b`                        | vLLM with LLaVA/InternVL | Small                         |
| Git server      | Adapter layer with provider abstraction  | ← same                  | Medium                         |
| Monitoring      | Local OTel+Grafana stack                 | External OTel Collector | Medium                         |
| Adapter layer   | Before PII scanner, `NormalizedDocument` | ← same                  | Large                          |
| Doc extraction  | markitdown (default)                     | markitdown + opt-in Docling on GPU | Small (when triggers fire — see T-6) |

The **provider abstraction (T-5)** and the **adapter layer (T-4)** are
the two strategically most important extensions: T-5 decouples the inference
backend choice from the code, T-4 decouples the data sources from the privacy core.
Both enable the dev environment (CPU, Ollama) and prod (GPU, vLLM/TEI)
to use the same codebase.
