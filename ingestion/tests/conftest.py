"""Shared fixtures for ingestion tests."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The startup OPA policy verification (issue #59 part 2) requires a live
# OPA instance. Unit tests that spin up FastAPI's TestClient would
# otherwise fail during the lifespan event. Set the opt-out before the
# app is imported anywhere in the test session.
os.environ.setdefault("SKIP_OPA_STARTUP_CHECK", "true")
# The ingestion-auth boot check (#126) refuses to import the app with
# AUTH_REQUIRED=true (the default) and an empty INGESTION_AUTH_TOKEN.
# Tests don't run a real ingestion service, so opt out at import time.
os.environ.setdefault("SKIP_INGESTION_AUTH_STARTUP_CHECK", "true")


@pytest.fixture
def mock_pg_pool():
    """AsyncMock of asyncpg.Pool."""
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    conn.fetch.return_value = []
    conn.fetchrow.return_value = None
    conn.execute.return_value = "INSERT 0 1"
    return pool


@pytest.fixture
def mock_scanner():
    """Mock PIIScanner that returns no PII by default."""
    from pii_scanner import PIIScanResult

    scanner = MagicMock()
    scanner.scan_text.return_value = PIIScanResult()
    scanner.mask_text.return_value = "masked text"
    scanner.pseudonymize_text.return_value = ("pseudonymized text", {})
    return scanner
