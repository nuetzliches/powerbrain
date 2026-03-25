"""
OpenAI-compatible LLM Provider abstraction.

Supports any backend that implements the OpenAI API:
Ollama (>=0.1.24), vLLM, HuggingFace TEI, infinity-embedding, OpenAI, etc.
"""

from __future__ import annotations

import httpx


class _BaseProvider:
    """Base class for OpenAI-compatible providers."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.headers: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )

    async def health_check(self, http: httpx.AsyncClient) -> bool:
        """Check provider health via GET /v1/models."""
        try:
            resp = await http.get(
                f"{self.base_url}/v1/models", headers=self.headers
            )
            return resp.status_code == 200
        except Exception:
            return False


class EmbeddingProvider(_BaseProvider):
    """Embeds text via POST /v1/embeddings (OpenAI-compatible)."""

    async def embed(
        self, http: httpx.AsyncClient, text: str, model: str
    ) -> list[float]:
        resp = await http.post(
            f"{self.base_url}/v1/embeddings",
            headers=self.headers,
            json={"model": model, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    async def embed_batch(
        self, http: httpx.AsyncClient, texts: list[str], model: str
    ) -> list[list[float]]:
        """Embed multiple texts in a single API call.

        Uses the OpenAI-compatible batch input format (input: list[str]).
        Results are sorted by index to guarantee input order.
        """
        if not texts:
            return []
        resp = await http.post(
            f"{self.base_url}/v1/embeddings",
            headers=self.headers,
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda x: x["index"])
        return [d["embedding"] for d in data]


class CompletionProvider(_BaseProvider):
    """Generates text via POST /v1/chat/completions (OpenAI-compatible)."""

    async def generate(
        self,
        http: httpx.AsyncClient,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str | None:
        resp = await http.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self.headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return content.strip() or None
