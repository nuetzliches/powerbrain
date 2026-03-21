"""Shared fixtures for reranker tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mock_model():
    """Mock CrossEncoder model."""
    model = MagicMock()
    model.predict.return_value = [0.9, 0.1, 0.5]
    return model
