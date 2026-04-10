"""Tests for mcp-server/manage_keys.py — pure utility functions."""

from manage_keys import generate_key, hash_key


class TestGenerateKey:
    def test_has_prefix(self):
        assert generate_key().startswith("pb_")

    def test_correct_length(self):
        key = generate_key()
        # pb_ (3 chars) + 64 hex chars = 67
        assert len(key) == 67

    def test_unique(self):
        k1 = generate_key()
        k2 = generate_key()
        assert k1 != k2


class TestHashKey:
    def test_deterministic(self):
        key = "pb_abc123"
        assert hash_key(key) == hash_key(key)

    def test_sha256_format(self):
        h = hash_key("pb_test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_keys_different_hashes(self):
        assert hash_key("pb_key1") != hash_key("pb_key2")
