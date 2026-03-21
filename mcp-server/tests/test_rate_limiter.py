"""Tests for TokenBucket rate limiter."""

import asyncio

import pytest

from server import TokenBucket


class TestTokenBucket:
    @pytest.fixture
    async def bucket(self):
        """Bucket with capacity 3, refill 1 token/sec."""
        return TokenBucket(capacity=3.0, refill_rate=1.0)

    async def test_initial_tokens_available(self, bucket):
        allowed, retry_after = await bucket.consume()
        assert allowed is True
        assert retry_after == 0.0

    async def test_exhaust_capacity(self, bucket):
        for _ in range(3):
            allowed, _ = await bucket.consume()
            assert allowed is True

        allowed, retry_after = await bucket.consume()
        assert allowed is False
        assert retry_after > 0.0

    async def test_refill_after_wait(self, bucket):
        for _ in range(3):
            await bucket.consume()
        await asyncio.sleep(1.1)
        allowed, _ = await bucket.consume()
        assert allowed is True

    async def test_capacity_cap(self, bucket):
        """Tokens should not exceed capacity even after long wait."""
        await asyncio.sleep(0.5)
        results = [await bucket.consume() for _ in range(4)]
        allowed_count = sum(1 for allowed, _ in results if allowed)
        assert allowed_count == 3

    async def test_retry_after_value(self):
        """retry_after should reflect time until next token."""
        bucket = TokenBucket(capacity=1.0, refill_rate=1.0)
        await bucket.consume()
        allowed, retry_after = await bucket.consume()
        assert allowed is False
        assert 0.0 < retry_after <= 1.0
