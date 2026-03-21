"""Shared fixtures for integration tests."""

import os
import pytest

def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless RUN_INTEGRATION_TESTS=1."""
    if os.getenv("RUN_INTEGRATION_TESTS") != "1":
        skip = pytest.mark.skip(reason="Set RUN_INTEGRATION_TESTS=1 to run")
        for item in items:
            item.add_marker(skip)
