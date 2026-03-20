"""
Reranker-Service
=================
Eigenständiger FastAPI-Microservice, der Qdrant-Ergebnisse mit einem
Cross-Encoder-Modell neu bewertet.

Baustein 5: Prometheus-Metriken werden über /metrics (prometheus-fastapi-instrumentator)
sowie custom Histogramme für Batch-Size und Modell-Ladezeit exponiert.
"""

import os
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder
from prometheus_client import (
    Counter, Histogram, make_asgi_app,
    CONTENT_TYPE_LATEST,
)
from starlette.responses import Response

# ── Konfiguration ────────────────────────────────────────────

MODEL_NAME = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
MAX_BATCH_SIZE  = int(os.getenv("RERANKER_MAX_BATCH",      "128"))
DEFAULT_TOP_N   = int(os.getenv("RERANKER_DEFAULT_TOP_N",  "10"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reranker")

# ── Prometheus Metriken ──────────────────────────────────────
reranker_requests_total = Counter(
    "kb_reranker_requests_total",
    "Gesamtzahl Reranking-Requests",
    ["status"],
)
reranker_duration = Histogram(
    "kb_reranker_duration_seconds",
    "Dauer eines Reranking-Requests",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)
reranker_batch_size = Histogram(
    "kb_reranker_batch_size",
    "Batch-Größe (Anzahl Dokumente) pro Request",
    buckets=[1, 5, 10, 20, 50, 100, 128],
)
reranker_model_load_seconds = Histogram(
    "kb_reranker_model_load_seconds",
    "Dauer des Modell-Ladens beim Start",
    buckets=[1, 5, 10, 20, 30, 60],
)

# ── Modell laden ─────────────────────────────────────────────

model: CrossEncoder | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    log.info(f"Lade Reranker-Modell: {MODEL_NAME}")
    t0 = time.time()
    model = CrossEncoder(MODEL_NAME, max_length=512)
    elapsed = time.time() - t0
    reranker_model_load_seconds.observe(elapsed)
    log.info(f"Modell geladen in {elapsed:.1f}s")
    yield
    log.info("Reranker-Service beendet")


app = FastAPI(
    title="KB Reranker Service",
    description="Cross-Encoder Reranking für die Wissensdatenbank",
    version="1.0.0",
    lifespan=lifespan,
)

# Prometheus ASGI-App unter /metrics einbinden
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


# ── Request/Response Modelle ─────────────────────────────────

class Document(BaseModel):
    id: str
    content: str
    score: float = Field(0.0)
    metadata: dict = Field(default_factory=dict)


class RerankRequest(BaseModel):
    query: str
    documents: list[Document]
    top_n: int = Field(DEFAULT_TOP_N)
    return_scores: bool = Field(True)


class RankedDocument(BaseModel):
    id: str
    content: str
    original_score: float
    rerank_score: float
    rank: int
    metadata: dict = Field(default_factory=dict)


class RerankResponse(BaseModel):
    results: list[RankedDocument]
    model: str
    query: str
    input_count: int
    output_count: int
    latency_ms: float


# ── Endpoints ────────────────────────────────────────────────

@app.post("/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest):
    if model is None:
        reranker_requests_total.labels(status="error").inc()
        raise HTTPException(status_code=503, detail="Modell noch nicht geladen")

    if len(request.documents) == 0:
        reranker_requests_total.labels(status="ok").inc()
        return RerankResponse(
            results=[], model=MODEL_NAME, query=request.query,
            input_count=0, output_count=0, latency_ms=0.0,
        )

    if len(request.documents) > MAX_BATCH_SIZE:
        reranker_requests_total.labels(status="error").inc()
        raise HTTPException(
            status_code=400,
            detail=f"Maximal {MAX_BATCH_SIZE} Dokumente pro Request"
        )

    t0 = time.time()
    reranker_batch_size.observe(len(request.documents))

    pairs  = [[request.query, doc.content] for doc in request.documents]
    scores = model.predict(pairs, show_progress_bar=False)

    scored_docs = sorted(
        [{"doc": doc, "rerank_score": float(s)} for doc, s in zip(request.documents, scores)],
        key=lambda x: x["rerank_score"],
        reverse=True,
    )

    top_n   = min(request.top_n, len(scored_docs))
    results = [
        RankedDocument(
            id=item["doc"].id,
            content=item["doc"].content,
            original_score=item["doc"].score,
            rerank_score=round(item["rerank_score"], 4),
            rank=rank,
            metadata=item["doc"].metadata,
        )
        for rank, item in enumerate(scored_docs[:top_n], start=1)
    ]

    latency = (time.time() - t0) * 1000
    reranker_duration.observe((time.time() - t0))
    reranker_requests_total.labels(status="ok").inc()

    log.info(
        f"Reranked {len(request.documents)} → {top_n} docs "
        f"in {latency:.0f}ms (query: {request.query[:50]})"
    )

    return RerankResponse(
        results=results, model=MODEL_NAME, query=request.query,
        input_count=len(request.documents), output_count=top_n,
        latency_ms=round(latency, 1),
    )


@app.get("/health")
async def health():
    return {"status": "ok" if model is not None else "loading", "model": MODEL_NAME}


@app.get("/models")
async def list_models():
    return {
        "current": MODEL_NAME,
        "alternatives": {
            "fast":          "cross-encoder/ms-marco-MiniLM-L-6-v2",
            "accurate":      "cross-encoder/ms-marco-MiniLM-L-12-v2",
            "multilingual":  "BAAI/bge-reranker-v2-m3",
        },
        "max_batch_size":  MAX_BATCH_SIZE,
        "default_top_n":   DEFAULT_TOP_N,
    }


# ── Startup ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)
