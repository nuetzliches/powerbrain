"""Shared fixtures for pb-proxy tests."""

import sys
from pathlib import Path

# Add pb-proxy to sys.path so bare imports (config, proxy, etc.) work
# with pytest's --import-mode=importlib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
