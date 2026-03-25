"""
In-process TTL cache for embedding vectors.

Sits between callers and EmbeddingProvider.embed(). Key is SHA-256
of "{model}:{text}" — deterministic and avoids storing raw text.

Backend: cachetools.TTLCache. Designed for future swap to Valkey
by replacing the get/set methods.

Configuration via env vars:
  EMBEDDING_CACHE_SIZE    (default: 2048)
  EMBEDDING_CACHE_TTL     (default: 3600)
  EMBEDDING_CACHE_ENABLED (default: true)
"""

from __future__ import annotations

import hashlib
import os
import threading

from cachetools import TTLCache


class EmbeddingCache:
    """Thread-safe TTL cache for embedding vectors."""

    def __init__(
        self,
        maxsize: int | None = None,
        ttl: int | None = None,
        enabled: bool | None = None,
    ):
        if maxsize is None:
            maxsize = int(os.getenv("EMBEDDING_CACHE_SIZE", "2048"))
        if ttl is None:
            ttl = int(os.getenv("EMBEDDING_CACHE_TTL", "3600"))
        if enabled is None:
            enabled = os.getenv("EMBEDDING_CACHE_ENABLED", "true").lower() == "true"

        self._enabled = enabled
        self._cache: TTLCache[str, list[float]] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(text: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{text}".encode()).hexdigest()

    def get(self, text: str, model: str) -> list[float] | None:
        if not self._enabled:
            return None
        key = self._key(text, model)
        with self._lock:
            val = self._cache.get(key)
            if val is not None:
                self._hits += 1
                return val
            self._misses += 1
            return None

    def set(self, text: str, model: str, vector: list[float]) -> None:
        if not self._enabled:
            return
        key = self._key(text, model)
        with self._lock:
            self._cache[key] = vector

    def stats(self) -> dict[str, int]:
        if not self._enabled:
            return {"hits": 0, "misses": 0, "size": 0}
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
            }
