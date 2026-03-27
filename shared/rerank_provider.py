"""
Configurable Reranker Provider abstraction.

Supports multiple backends:
- **powerbrain** (default): Built-in Cross-Encoder service (reranker/service.py)
- **tei**: HuggingFace Text Embeddings Inference /rerank endpoint
- **cohere**: Cohere Rerank API v2

Each provider translates between the uniform RerankDocument format and the
backend-specific API, so the MCP server does not need to know which backend
is active.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import httpx


# ---------------------------------------------------------------------------
# Uniform document type
# ---------------------------------------------------------------------------

@dataclass
class RerankDocument:
    """Uniform document for rerank input and output."""

    id: str
    content: str
    score: float = 0.0
    rerank_score: float = 0.0
    rank: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------

class _BaseRerankProvider:
    """Base class for reranker backends."""

    def __init__(self, base_url: str, api_key: str = "", model: str = ""):
        self.base_url = base_url.rstrip("/")
        self.headers: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )
        self.model = model

    async def health_check(self, http: httpx.AsyncClient) -> bool:
        """Check provider health via GET /health."""
        try:
            resp = await http.get(
                f"{self.base_url}/health", headers=self.headers
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def rerank(
        self,
        http: httpx.AsyncClient,
        query: str,
        documents: list[RerankDocument],
        top_n: int,
    ) -> list[RerankDocument]:
        """Rerank documents. Subclasses must implement this."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Powerbrain (built-in Cross-Encoder service)
# ---------------------------------------------------------------------------

class PowerbrainRerankProvider(_BaseRerankProvider):
    """Calls the built-in reranker service (reranker/service.py)."""

    async def rerank(
        self,
        http: httpx.AsyncClient,
        query: str,
        documents: list[RerankDocument],
        top_n: int,
    ) -> list[RerankDocument]:
        if not documents:
            return []

        resp = await http.post(
            f"{self.base_url}/rerank",
            headers=self.headers,
            json={
                "query": query,
                "documents": [
                    {
                        "id": doc.id,
                        "content": doc.content,
                        "score": doc.score,
                        "metadata": doc.metadata,
                    }
                    for doc in documents
                ],
                "top_n": top_n,
                "return_scores": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        return [
            RerankDocument(
                id=r["id"],
                content=r["content"],
                score=r["original_score"],
                rerank_score=r["rerank_score"],
                rank=r["rank"],
                metadata=r.get("metadata", {}),
            )
            for r in data["results"]
        ]


# ---------------------------------------------------------------------------
# HuggingFace TEI (/rerank)
# ---------------------------------------------------------------------------

class TEIRerankProvider(_BaseRerankProvider):
    """Calls HuggingFace Text Embeddings Inference /rerank endpoint.

    TEI request:  {query, texts: [str], raw_scores: false, truncate: true}
    TEI response: [{index: int, score: float}]
    """

    async def rerank(
        self,
        http: httpx.AsyncClient,
        query: str,
        documents: list[RerankDocument],
        top_n: int,
    ) -> list[RerankDocument]:
        if not documents:
            return []

        resp = await http.post(
            f"{self.base_url}/rerank",
            headers=self.headers,
            json={
                "query": query,
                "texts": [doc.content for doc in documents],
                "raw_scores": False,
                "truncate": True,
            },
        )
        resp.raise_for_status()
        results = resp.json()

        # Map back via index, sort by score descending, truncate
        scored = sorted(results, key=lambda r: r["score"], reverse=True)[:top_n]

        return [
            RerankDocument(
                id=documents[r["index"]].id,
                content=documents[r["index"]].content,
                score=documents[r["index"]].score,
                rerank_score=round(r["score"], 4),
                rank=rank,
                metadata=documents[r["index"]].metadata,
            )
            for rank, r in enumerate(scored, start=1)
        ]


# ---------------------------------------------------------------------------
# Cohere Rerank v2
# ---------------------------------------------------------------------------

class CohereRerankProvider(_BaseRerankProvider):
    """Calls Cohere Rerank API v2.

    Cohere request:  {model, query, documents: [str], top_n, return_documents: false}
    Cohere response: {results: [{index: int, relevance_score: float}]}
    """

    async def rerank(
        self,
        http: httpx.AsyncClient,
        query: str,
        documents: list[RerankDocument],
        top_n: int,
    ) -> list[RerankDocument]:
        if not documents:
            return []

        resp = await http.post(
            f"{self.base_url}/v2/rerank",
            headers=self.headers,
            json={
                "model": self.model,
                "query": query,
                "documents": [doc.content for doc in documents],
                "top_n": top_n,
                "return_documents": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        # Results are already sorted by relevance_score descending
        return [
            RerankDocument(
                id=documents[r["index"]].id,
                content=documents[r["index"]].content,
                score=documents[r["index"]].score,
                rerank_score=round(r["relevance_score"], 4),
                rank=rank,
                metadata=documents[r["index"]].metadata,
            )
            for rank, r in enumerate(data["results"], start=1)
        ]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[_BaseRerankProvider]] = {
    "powerbrain": PowerbrainRerankProvider,
    "tei": TEIRerankProvider,
    "cohere": CohereRerankProvider,
}


def create_rerank_provider(
    backend: str = "powerbrain",
    base_url: str = "http://reranker:8082",
    api_key: str = "",
    model: str = "",
) -> _BaseRerankProvider:
    """Create a reranker provider for the given backend.

    Args:
        backend: One of 'powerbrain', 'tei', 'cohere'.
        base_url: Base URL of the reranker service.
        api_key: Optional API key for authenticated providers.
        model: Model name (required for Cohere, ignored for others).

    Raises:
        ValueError: If backend is not recognized.
    """
    cls = _PROVIDERS.get(backend)
    if cls is None:
        supported = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unknown reranker backend: {backend!r}. Supported: {supported}"
        )
    if backend == "cohere" and not model:
        raise ValueError("Cohere reranker backend requires a model name (e.g., 'rerank-v3.5')")
    return cls(base_url=base_url, api_key=api_key, model=model)
