"""Shared fixtures for mcp-server tests."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add mcp-server to path so we can import server, graph_service
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The lifespan OPA policy verification (issue #59 part 2) requires a
# running OPA server. Unit tests that exercise the ASGI stack via
# TestClient would otherwise fail at startup. Set the opt-out before
# any test imports the module under test.
os.environ.setdefault("SKIP_OPA_STARTUP_CHECK", "true")
# The ingestion-auth boot check (#126) refuses to import the module with
# AUTH_REQUIRED=true (the default) and an empty INGESTION_AUTH_TOKEN.
os.environ.setdefault("SKIP_INGESTION_AUTH_STARTUP_CHECK", "true")


@pytest.fixture
def mock_pg_pool():
    """AsyncMock of asyncpg.Pool with acquire context manager."""
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    conn.fetch.return_value = []
    conn.fetchrow.return_value = None
    conn.execute.return_value = "INSERT 0 1"
    return pool


@pytest.fixture
def mock_http_client():
    """AsyncMock of httpx.AsyncClient for direct patching."""
    client = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {}
    client.post.return_value = response
    return client
