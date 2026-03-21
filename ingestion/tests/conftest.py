"""Shared fixtures for ingestion tests."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
