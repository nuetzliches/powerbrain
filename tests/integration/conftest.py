"""Shared fixtures for integration tests."""

import os
from pathlib import Path
import pytest

_INTEGRATION_DIR = str(Path(__file__).resolve().parent)

def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless RUN_INTEGRATION_TESTS=1."""
    if os.getenv("RUN_INTEGRATION_TESTS") != "1":
        skip = pytest.mark.skip(reason="Set RUN_INTEGRATION_TESTS=1 to run")
        for item in items:
            if str(Path(item.fspath).resolve()).startswith(_INTEGRATION_DIR):
                item.add_marker(skip)
