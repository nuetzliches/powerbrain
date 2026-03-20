#!/usr/bin/env python3
"""
DEPRECATED — Use testdata/seed.py instead.

The testdata seed has moved to testdata/ and now uses the Ingestion API
instead of writing directly to Qdrant. To seed test data:

    # Via Docker Compose (recommended):
    docker compose --profile seed up -d

    # Manually (requires all services running):
    python3 testdata/seed.py

The test documents are defined in testdata/documents.json.
"""

import sys

print(
    "DEPRECATED: This script has been replaced by testdata/seed.py\n"
    "\n"
    "Use one of:\n"
    "  docker compose --profile seed up -d\n"
    "  python3 testdata/seed.py\n",
    file=sys.stderr,
)
sys.exit(1)
