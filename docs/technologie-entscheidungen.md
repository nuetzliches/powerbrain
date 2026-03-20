# Technologie-Entscheidungen & Erweiterungsoptionen

Diskutierte Architekturentscheidungen und deren Begründung.

---

## T-1: Vision Language Models (VLM) neben Ollama

### Frage

Können wir zusätzlich zu Ollama ein lokales VLM (Vision Language Model)
für multimodale Inhalte (Bilder, Diagramme, PDFs mit Grafiken) anbinden?

### Antwort: Ja — Ollama selbst unterstützt VLMs

Ollama kann nativ Vision-Modelle hosten. Kein separater Service nötig:

| Modell            | Stärke                   | RAM-Bedarf |
| ----------------- | ------------------------ | ---------- |
| `llava:7b`        | Allgemein, gute Balance  | ~6 GB      |
| `llama3.2-vision` | Texterkennung in Bildern | ~6 GB      |
| `moondream2`      | Sehr schnell, kompakt    | ~2 GB      |
| `llava:34b`       | Hohe Qualität, langsam   | ~25 GB     |

### Konzeptionelle Abgrenzung (wichtig)

```
Bild/Diagramm
    ↓
VLM (llava)          → erzeugt Textbeschreibung: "Architekturdiagramm mit 3 Services..."
    ↓
Embedding (nomic-embed-text)  → Vektor [0.12, -0.34, ...]
    ↓
Qdrant
```

**VLM ≠ multimodales Embedding.** Das VLM _beschreibt_ das Bild in Text,
der anschließend normal embedded wird. Man sucht semantisch über die
Beschreibung, nicht direkt über Pixel.

Alternative: CLIP-basierte Bild-Embeddings (Bild direkt → Vektor). Vorteil:
schneller. Nachteil: eigener Embedding-Service, andere Vektordimension,
eigene Qdrant-Collection. Für dieses Projekt nicht empfohlen.

### Implementierung

**Ingestion-Pipeline** (neues VLM-Modul):

```python
async def describe_image(image_bytes: bytes, mime_type: str) -> str:
    """Sendet Bild an Ollama VLM, erhält Textbeschreibung zurück."""
    resp = await http.post(f"{OLLAMA_URL}/api/generate", json={
        "model": VLM_MODEL,  # env: VLM_MODEL=llava:7b
        "prompt": "Beschreibe dieses Bild präzise auf Deutsch...",
        "images": [base64.b64encode(image_bytes).decode()],
        "stream": False,
    })
    return resp.json()["response"]
```

Eingebunden im Adapter-Layer (→ T-4) beim Verarbeiten von:

- PDF-Seiten mit Grafiken (via `pymupdf`)
- Standalone-Bilddateien
- DOCX/PPTX mit eingebetteten Diagrammen

**Konfiguration** (`.env`):

```
VLM_ENABLED=false          # default aus, opt-in
VLM_MODEL=llava:7b
VLM_MAX_IMAGE_SIZE_MB=10
```

### Ollama-Container Anpassung

Kein zusätzlicher Container. Modell muss einmalig geladen werden:

```bash
docker exec kb-ollama ollama pull llava:7b
```

---

## T-2: Git-Server-Unterstützung (Forgejo als Beispiel)

### Frage

Ist Forgejo ein festes Requirement, oder funktionieren alle Git-basierten Server?

### Antwort: Forgejo ist ein Beispiel — alle gängigen Git-Server funktionieren

Das Projekt hat zwei Integrationspunkte mit Git-Servern:

#### Integrationspunkt 1: OPA Bundle-Polling

OPA pollt Policies als HTTP-Bundle. Das ist rein URL-basiert — jeder
Server der eine `.tar.gz` über HTTP ausliefert funktioniert:

```yaml
# OPA-Config — server-agnostisch:
services:
  git-server:
    url: ${GIT_SERVER_URL}
    credentials:
      bearer:
        token: ${GIT_TOKEN}
bundles:
  kb:
    service: git-server
    resource: /api/v1/repos/org/kb-policies/raw/bundle.tar.gz # Pfad anpassen
    polling:
      min_delay_seconds: 10
```

| Server          | Bundle-URL-Schema                                                             |
| --------------- | ----------------------------------------------------------------------------- |
| Forgejo/Gitea   | `/api/v1/repos/{org}/{repo}/raw/{file}`                                       |
| GitHub          | `/raw/{org}/{repo}/{branch}/{file}` (über API oder raw.githubusercontent.com) |
| GitLab          | `/api/v4/projects/{id}/repository/files/{file}/raw`                           |
| Bitbucket Cloud | `/2.0/repositories/{ws}/{repo}/src/{branch}/{file}`                           |

#### Integrationspunkt 2: Code-Ingestion (Repo-Inhalte lesen)

Aktuell Forgejo-API-Pfade. Mit dem Adapter-Layer (→ T-4) wird das abstrahiert.

### Empfohlene Konfiguration

```env
# Statt FORGEJO_URL/FORGEJO_TOKEN:
GIT_SERVER_TYPE=forgejo   # forgejo | github | gitlab | bitbucket
GIT_SERVER_URL=https://git.intern.example.com
GIT_TOKEN=...
GIT_ORG=kb-org
```

Der Git-Adapter (→ T-4) übersetzt auf den jeweiligen API-Dialekt.

---

## T-3: Monitoring — Externer OTel Collector, optionaler Grafana-Stack

### Frage

Kann das Monitoring über einen externen OpenTelemetry Collector angeboten
werden, so dass der lokale Grafana-Stack optional wird?

### Antwort: Ja — das ist sogar die empfohlene Architektur

Der **OpenTelemetry Collector** ist die standardisierte Routing-Schicht zwischen
Services und Observability-Backends:

```
Services (MCP, Reranker, Ingestion)
    │ OTLP (gRPC :4317)
    ▼
┌─────────────────────────────┐
│  OTel Collector             │
│  ├─ Receiver: OTLP          │
│  ├─ Processor: batch, attrs │
│  └─ Exporters:              │
│     ├─ Tempo (lokal)        │  ← optional
│     ├─ Grafana Cloud        │  ← oder extern
│     ├─ Datadog              │  ← oder extern
│     └─ Jaeger               │  ← oder extern
└─────────────────────────────┘
```

Services kennen nur `OTLP_ENDPOINT` — sie sind backend-agnostisch.

### Docker Compose Profiles

```yaml
# docker-compose.yml
services:
  otel-collector:          # immer aktiv (leichtgewichtig)
    image: otel/opentelemetry-collector-contrib:latest
    profiles: []           # kein Profile = immer gestartet
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

**Betrieb mit lokalem Stack:**

```bash
docker compose --profile monitoring-local up -d
```

**Betrieb mit externem Backend (z.B. Grafana Cloud):**

```bash
# .env:
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic ...
docker compose up -d   # nur otel-collector, kein lokaler Stack
```

### OTel Collector Konfiguration (`monitoring/otel-collector.yml`)

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
        value: kb
        action: upsert

exporters:
  # Lokales Tempo (wenn monitoring-local Profile aktiv)
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true

  # Optionales externes Backend
  otlp/external:
    endpoint: ${OTEL_EXTERNAL_ENDPOINT:-}
    headers:
      authorization: ${OTEL_EXTERNAL_AUTH:-}

  # Prometheus-kompatible Metriken (für Scraping)
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

### Prometheus-Metriken: Scraping vs. Push

Aktuell: Prometheus _scrapet_ Services aktiv (`/metrics`-Endpoint).
Alternative: Services _pushen_ Metriken per OTLP an den Collector.

|                     | Scraping (aktuell)            | OTLP Push                   |
| ------------------- | ----------------------------- | --------------------------- |
| Prometheus nötig    | Ja                            | Nein (Collector reicht)     |
| Pull-Modell         | Ja                            | Nein                        |
| Firewall-freundlich | Weniger                       | Ja (Collector ausgehend)    |
| Empfehlung          | Einfacher für lokalen Betrieb | Besser für externe Backends |

**Empfehlung:** Beide parallel anbieten — Scraping bleibt als Fallback,
OTLP-Push für externe Backends. Services brauchen beide Methoden nicht zu wissen:
Der Collector ist der einzige Endpunkt.

---

## T-4: Adapter-Schicht für zusätzliche Datenquellen

### Frage

Soll eine Adapter-Schicht für zusätzliche Datenquellen implementiert werden?
Wo ist sie anzusiedeln — vor oder nach der Datenschutz-Schicht?

### Antwort: Ja — zwingend vor der Datenschutz-Schicht

### Begründung: Warum VOR dem PII-Scanner

```
Externe Quelle (PDF, Git, XLSX, API, ...)
    ↓
[ADAPTER-SCHICHT]          ← Normalisierung: Binär → Text + Metadaten
    ↓
[PII-Scanner (Presidio)]   ← erste Schicht die Text sieht
    ↓
[OPA Policy]               ← Klassifizierung + Zweckprüfung
    ↓
[Embedding (Ollama)]
    ↓
[Qdrant]
```

Der PII-Scanner kann nur Text verarbeiten. Würde man PDFs/DOCX direkt in
die Privacy-Schicht geben, müsste dort Dokumenten-Parsing stattfinden —
das verletzt das Single-Responsibility-Prinzip. Die Adapter-Schicht liefert
immer `NormalizedDocument`, egal welche Quelle.

### Gemeinsame Abstraktion

```python
# ingestion/adapters/base.py

from dataclasses import dataclass, field
from typing import AsyncIterator

@dataclass
class NormalizedDocument:
    content: str                    # Extrahierter Volltext
    content_type: str               # "text", "code", "table", "image_description"
    source_ref: str                 # Original-URI/Pfad
    source_type: str                # "git", "pdf", "xlsx", "api", ...
    language: str | None = None     # "de", "en", None = unbekannt
    metadata: dict = field(default_factory=dict)
    # Enthält: title, author, created_at, repo, path, etc.
    chunks: list[str] | None = None  # Optional: vorgeschnittene Chunks

class SourceAdapter:
    """Abstrakte Basis für alle Datenquellen-Adapter."""

    async def fetch(self) -> AsyncIterator[NormalizedDocument]:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError
```

### Geplante Adapter

| Adapter             | Quellen                                   | Priorität |
| ------------------- | ----------------------------------------- | --------- |
| `GitAdapter`        | Forgejo, GitHub, GitLab, Bitbucket, Gitea | Hoch      |
| `FileAdapter`       | PDF, DOCX, XLSX, Markdown, TXT            | Hoch      |
| `DatabaseAdapter`   | PostgreSQL-Dump, CSV, JSON                | Mittel    |
| `ConfluenceAdapter` | Confluence REST API                       | Mittel    |
| `WebAdapter`        | HTTP/HTML-Scraping, Sitemaps              | Niedrig   |
| `KafkaAdapter`      | Streaming-Inhalte                         | Niedrig   |
| `EmailAdapter`      | IMAP, EML-Dateien                         | Niedrig   |

### Git-Adapter (löst auch T-2)

```python
# ingestion/adapters/git_adapter.py

class GitAdapter(SourceAdapter):
    """Unterstützt Forgejo, Gitea, GitHub, GitLab, Bitbucket."""

    PROVIDERS = {
        "forgejo": ForgejoProvider,
        "gitea":   ForgejoProvider,   # API-kompatibel
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

### Verzeichnisstruktur

```
ingestion/
├── ingestion_api.py         ← FastAPI-App (zu implementieren, P0-2)
├── adapters/
│   ├── base.py              ← NormalizedDocument, SourceAdapter
│   ├── git_adapter.py       ← Git-Server (alle Provider)
│   ├── file_adapter.py      ← PDF/DOCX/XLSX via pymupdf/python-docx
│   ├── database_adapter.py  ← CSV/JSON/PG-Dumps
│   └── providers/
│       ├── forgejo.py
│       ├── github.py
│       ├── gitlab.py
│       └── bitbucket.py
├── pii_scanner.py           ← unverändert
├── retention_cleanup.py     ← unverändert
└── snapshot_service.py      ← unverändert
```

### Zusammenspiel mit VLM (T-1)

Der `FileAdapter` ruft bei Bildinhalten automatisch das VLM auf:

```python
# In file_adapter.py:
if page.has_images and VLM_ENABLED:
    for image in page.images:
        description = await vlm.describe_image(image.bytes)
        # description wird dem Chunk-Text hinzugefügt
        chunk_text += f"\n[Abbildung: {description}]"
```

Das VLM ist damit transparent in die Ingestion-Pipeline integriert —
kein separater MCP-Tool-Aufruf nötig.

---

---

## T-5: vLLM als Alternative zu Ollama

### Frage

Kann vLLM Ollama ersetzen, und wann ist das sinnvoll?

### Vergleich

|                       | Ollama                                  | vLLM                                        |
| --------------------- | --------------------------------------- | ------------------------------------------- |
| **Primärzweck**       | Developer-Experience, lokale Nutzung    | Production-Grade LLM Serving                |
| **Throughput**        | Sequenziell, niedrig                    | Continuous Batching → 10–50× höher          |
| **GPU-Memory**        | Standard-Allokation                     | PagedAttention → deutlich effizienter       |
| **Parallelität**      | Schlecht (ein Request nach dem anderen) | Sehr gut (dozens simultaneous)              |
| **CPU-only**          | ✅ Gut                                  | ⚠️ Experimentell, sehr langsam              |
| **API**               | Ollama-spezifisch + OpenAI-compat.      | OpenAI-kompatibel (nativ)                   |
| **VLM-Support**       | LLaVA, moondream2, llama3.2-vision      | LLaVA, InternVL, Qwen-VL — besser gebaztcht |
| **Embedding-Modelle** | Breit (nomic, mxbai, all-minilm ...)    | Eingeschränkt                               |
| **Multi-GPU**         | Nein                                    | Tensor Parallelism, ja                      |
| **Setup**             | `ollama pull modell`                    | CUDA + Docker + Modell-Konfiguration        |

### Das Problem mit direktem Ersatz

vLLM ist **kein vollwertiger Embedding-Service**. Es unterstützt `/v1/embeddings`,
aber die Embedding-Modell-Auswahl ist schmaler als bei Ollama. `nomic-embed-text`
ist nicht garantiert verfügbar. Für hochperformante Embeddings gibt es bessere
dedizierte Alternativen:

| Service                                         | Stärke                                             |
| ----------------------------------------------- | -------------------------------------------------- |
| **HuggingFace Text Embeddings Inference (TEI)** | Viele Modelle, sehr schnell, OpenAI-compat.        |
| **infinity-embedding**                          | Lightweight, OpenAI-compat., einfaches Setup       |
| **Ollama**                                      | Einfach, breite Modell-Unterstützung, CPU-tauglich |

### Empfohlene Aufteilung

```
Embeddings:  infinity (Prod/GPU)
LLM/VLM:     vLLM (Prod/GPU)
```

Beide Rollen über getrennte Endpunkte konfigurierbar:

```env
EMBEDDING_PROVIDER_URL=http://infinity:80
EMBEDDING_MODEL=nomic-embed-text

LLM_PROVIDER_URL=http://vllm:8000
LLM_MODEL=llama3.2-vision                   # für VLM
```

### Provider-Abstraktion (LLMProvider Interface)

Da sowohl Ollama (OpenAI-kompatibler Modus), vLLM, HF TEI als auch externe
Dienste eine OpenAI-kompatible API anbieten, reicht eine dünne Abstraktion:

```python
# mcp-server/llm_provider.py  und  ingestion/llm_provider.py

import httpx

class LLMProvider:
    """
    Dünne Abstraktion über OpenAI-kompatible LLM/Embedding-Endpunkte.
    Unterstützt: Ollama, vLLM, HF TEI, infinity, OpenAI (mit AVV!).
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
        """Textgenerierung, optional mit Bildern (VLM)."""
        messages = [{"role": "user", "content": prompt}]
        if images:
            # OpenAI Vision-Format
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

**Ollama-Kompatibilität:** Ollama exponiert seit v0.1.24 eine OpenAI-kompatible
API unter `/v1/` — der Provider-Code funktioniert damit ohne Änderungen.

### Wann vLLM wählen

- Mehr als 3–5 gleichzeitige Agents (Parallelitäts-Bottleneck bei Ollama)
- GPU verfügbar (vLLM ist auf CUDA optimiert, CPU sehr langsam)
- VLM unter Last (mehrere gleichzeitige Bild-Beschreibungen in Ingestion-Batch)
- Modelle > 13B Parameter (PagedAttention spart entscheidend GPU-RAM)

### Docker Compose Erweiterung

```yaml
# ── vLLM (optional, ersetzt Ollama für LLM/VLM) ──────────
vllm:
  image: vllm/vllm-openai:latest
  container_name: kb-vllm
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
    - kb-net
  restart: unless-stopped

# ── HF Text Embeddings Inference (optional) ──────────────
tei:
  image: ghcr.io/huggingface/text-embeddings-inference:latest
  container_name: kb-tei
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
    - kb-net
  restart: unless-stopped
```

**Betrieb mit GPU-Stack:**

```bash
docker compose --profile gpu up -d
# .env:
# EMBEDDING_PROVIDER_URL=http://tei:80
# LLM_PROVIDER_URL=http://vllm:8000
# LLM_MODEL=llava-hf/llava-1.5-7b-hf
```

---

## Zusammenfassung der Empfehlungen

| Thema           | Dev (CPU)                                | Prod (GPU)              | Aufwand                      |
| --------------- | ---------------------------------------- | ----------------------- | ---------------------------- |
| Embeddings      | Ollama                                   | HF TEI oder infinity    | Klein (Provider-Abstraktion) |
| LLM/VLM         | Ollama                                   | vLLM                    | Mittel (Docker Profile)      |
| VLM-Integration | Ollama `llava:7b`                        | vLLM mit LLaVA/InternVL | Klein                        |
| Git-Server      | Adapter-Schicht mit Provider-Abstraktion | ← gleich                | Mittel                       |
| Monitoring      | Lokaler OTel+Grafana-Stack               | Externer OTel Collector | Mittel                       |
| Adapter-Schicht | Vor PII-Scanner, `NormalizedDocument`    | ← gleich                | Groß                         |

Die **Provider-Abstraktion (T-5)** und die **Adapter-Schicht (T-4)** sind
die zwei strategisch wichtigsten Erweiterungen: T-5 entkoppelt die Inference-
Backend-Wahl vom Code, T-4 entkoppelt die Datenquellen vom Privacy-Kern.
Beide ermöglichen, dass Dev-Umgebung (CPU, Ollama) und Prod (GPU, vLLM/TEI)
dieselbe Codebasis nutzen.
