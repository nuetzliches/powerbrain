"""Shared fixtures for worker tests."""

import sys
from pathlib import Path

# Add repo root so ``import worker.*`` works without installation
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
