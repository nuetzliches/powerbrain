"""Verify PG connection pool is initialized in a lifespan context manager
with a startup healthcheck — P2-5 fix."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_FILE = ROOT / "mcp-server" / "server.py"


class TestPGPoolLifespan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_FILE.read_text(encoding="utf-8")

    def test_lifespan_creates_pg_pool(self):
        """PG pool must be created in a lifespan context, not lazily."""
        self.assertIn("asyncpg.create_pool", self.source)
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("create_pool", lifespan_section,
                       "asyncpg.create_pool must be called inside the lifespan function")

    def test_startup_healthcheck(self):
        """Lifespan must run SELECT 1 as a startup healthcheck."""
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("SELECT 1", lifespan_section,
                       "Lifespan must run 'SELECT 1' as startup healthcheck")

    def test_pool_closed_on_shutdown(self):
        """PG pool must be closed in lifespan shutdown."""
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("pg_pool.close()", lifespan_section,
                       "Lifespan must close the PG pool on shutdown")

    def test_get_pg_pool_not_lazy(self):
        """get_pg_pool must not lazily create the pool anymore."""
        func_start = self.source.index("def get_pg_pool")
        func_end = self.source.index("\n\n", func_start)
        func_body = self.source[func_start:func_end]
        self.assertNotIn("create_pool", func_body,
                          "get_pg_pool must not lazily create the pool")

    def test_http_client_closed_on_shutdown(self):
        """HTTP client should also be closed in lifespan shutdown."""
        lifespan_start = self.source.index("async def lifespan")
        lifespan_section = self.source[lifespan_start:lifespan_start + 800]
        self.assertIn("http.aclose()", lifespan_section,
                       "Lifespan should close the httpx client on shutdown")

    def test_lifespan_bypass_exists(self):
        """LifespanBypass must route lifespan events past auth middleware."""
        self.assertIn("class LifespanBypass", self.source,
                       "LifespanBypass class must exist to bypass auth on lifespan events")
        bypass_start = self.source.index("class LifespanBypass")
        bypass_section = self.source[bypass_start:bypass_start + 400]
        self.assertIn('"lifespan"', bypass_section,
                       "LifespanBypass must check for lifespan scope type")
        self.assertIn("starlette_app", bypass_section,
                       "LifespanBypass must route lifespan to starlette_app (no auth)")

    def test_lifespan_bypass_is_final_app(self):
        """LifespanBypass must be the app passed to uvicorn.run."""
        # After LifespanBypass is defined, app must be reassigned to it
        bypass_start = self.source.index("class LifespanBypass")
        after_bypass = self.source[bypass_start:]
        self.assertIn("app = LifespanBypass()", after_bypass,
                       "app must be set to LifespanBypass() before uvicorn.run")


if __name__ == "__main__":
    unittest.main()
