"""Shared fixtures for pb-proxy tests."""

import os
import sys
from pathlib import Path

# Add pb-proxy to sys.path so bare imports (config, proxy, etc.) work
# with pytest's --import-mode=importlib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip the lifespan OPA policy verification (issue #59 part 2) in unit
# tests — TestClient would otherwise fail to start because no OPA is
# running in the CI container.
os.environ.setdefault("SKIP_OPA_STARTUP_CHECK", "true")
# The ingestion-auth boot check (#126) refuses to import config.py with
# AUTH_REQUIRED=true (the default) and an empty INGESTION_AUTH_TOKEN.
os.environ.setdefault("SKIP_INGESTION_AUTH_STARTUP_CHECK", "true")
