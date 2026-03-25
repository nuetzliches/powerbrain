"""Tests for shared.embedding_cache module."""

from __future__ import annotations

import time

import pytest

from shared.embedding_cache import EmbeddingCache


class TestEmbeddingCacheGetSet:
    def test_miss_returns_none(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        assert cache.get("hello", "model-a") is None

    def test_set_then_get(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        vec = [0.1, 0.2, 0.3]
        cache.set("hello", "model-a", vec)
        assert cache.get("hello", "model-a") == vec

    def test_different_models_are_separate_keys(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        cache.set("hello", "model-a", [1.0])
        cache.set("hello", "model-b", [2.0])
        assert cache.get("hello", "model-a") == [1.0]
        assert cache.get("hello", "model-b") == [2.0]

    def test_different_texts_are_separate_keys(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        cache.set("hello", "m", [1.0])
        cache.set("world", "m", [2.0])
        assert cache.get("hello", "m") == [1.0]
        assert cache.get("world", "m") == [2.0]


class TestEmbeddingCacheEviction:
    def test_maxsize_eviction(self):
        cache = EmbeddingCache(maxsize=2, ttl=60)
        cache.set("a", "m", [1.0])
        cache.set("b", "m", [2.0])
        cache.set("c", "m", [3.0])
        # "a" should be evicted (LRU)
        assert cache.get("a", "m") is None
        assert cache.get("c", "m") == [3.0]

    def test_ttl_expiry(self):
        cache = EmbeddingCache(maxsize=10, ttl=1)
        cache.set("hello", "m", [1.0])
        assert cache.get("hello", "m") == [1.0]
        time.sleep(1.1)
        assert cache.get("hello", "m") is None


class TestEmbeddingCacheStats:
    def test_initial_stats(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        assert cache.stats() == {"hits": 0, "misses": 0, "size": 0}

    def test_hit_miss_counting(self):
        cache = EmbeddingCache(maxsize=10, ttl=60)
        cache.set("a", "m", [1.0])
        cache.get("a", "m")       # hit
        cache.get("b", "m")       # miss
        cache.get("a", "m")       # hit
        stats = cache.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["size"] == 1


class TestEmbeddingCacheDisabled:
    def test_disabled_cache_returns_none(self):
        cache = EmbeddingCache(maxsize=10, ttl=60, enabled=False)
        cache.set("a", "m", [1.0])
        assert cache.get("a", "m") is None

    def test_disabled_cache_stats_empty(self):
        cache = EmbeddingCache(maxsize=10, ttl=60, enabled=False)
        cache.set("a", "m", [1.0])
        cache.get("a", "m")
        assert cache.stats() == {"hits": 0, "misses": 0, "size": 0}
