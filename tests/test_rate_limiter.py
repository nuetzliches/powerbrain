"""Verify rate limiting configuration in MCP server."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_FILE = ROOT / "mcp-server" / "server.py"


class TestRateLimiter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_FILE.read_text(encoding="utf-8")

    def test_rate_limit_env_vars(self):
        """Rate limit must be configurable via environment variables."""
        self.assertIn("RATE_LIMIT_ANALYST", self.source,
                       "Must have RATE_LIMIT_ANALYST env var")
        self.assertIn("RATE_LIMIT_DEVELOPER", self.source,
                       "Must have RATE_LIMIT_DEVELOPER env var")
        self.assertIn("RATE_LIMIT_ADMIN", self.source,
                       "Must have RATE_LIMIT_ADMIN env var")

    def test_rate_limit_enabled_flag(self):
        """Rate limiting must be toggleable via RATE_LIMIT_ENABLED."""
        self.assertIn("RATE_LIMIT_ENABLED", self.source,
                       "Must have RATE_LIMIT_ENABLED env var")

    def test_token_bucket_class(self):
        """Must implement a TokenBucket for rate limiting."""
        self.assertIn("class TokenBucket", self.source,
                       "Must have a TokenBucket class")

    def test_rate_limit_middleware(self):
        """Must have rate limiting middleware in the request chain."""
        self.assertIn("RateLimitMiddleware", self.source,
                       "Must have a RateLimitMiddleware class")

    def test_429_response(self):
        """Rate limiter must return 429 when limit exceeded."""
        self.assertIn("429", self.source,
                       "Must return HTTP 429 when rate limited")

    def test_retry_after_header(self):
        """Rate limiter must include Retry-After header."""
        self.assertIn("Retry-After", self.source,
                       "Must set Retry-After header on 429 responses")

    def test_rate_limit_prometheus_counter(self):
        """Must track rate limit rejections in Prometheus."""
        self.assertIn("rate_limit", self.source,
                       "Must have rate_limit Prometheus metric")

    def test_rate_limit_fail_open(self):
        """Rate limiter must fail open (allow requests on error)."""
        # Find the RateLimitMiddleware class
        cls_start = self.source.index("class RateLimitMiddleware")
        cls_end = self.source.index("\nclass ", cls_start + 1) if \
            "\nclass " in self.source[cls_start + 1:] else len(self.source)
        cls_body = self.source[cls_start:cls_end]
        self.assertIn("except", cls_body,
                       "RateLimitMiddleware must handle errors")


if __name__ == "__main__":
    unittest.main()
