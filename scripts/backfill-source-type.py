#!/usr/bin/env python3
"""
Backfill source_type payload field in Qdrant for existing points.

Derives source_type from the existing 'source' metadata field:
  - "document:inline" → source_type = "document"
  - "git-commit:inline" → source_type = "git-commit"

Uses Qdrant REST API directly. Run inside the pb-net Docker network:
  docker run --rm --network powerbrain_pb-net \
    -v $(pwd)/scripts:/scripts python:3-slim \
    python /scripts/backfill-source-type.py

Env vars:
  QDRANT_URL   — default: http://qdrant:6333
  COLLECTION   — default: pb_general
  DRY_RUN      — set to "true" for read-only mode
"""

import json
import os
import urllib.request
import urllib.error

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.environ.get("COLLECTION", "pb_general")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
BATCH_SIZE = 100


def qdrant_request(method: str, path: str, data: dict | None = None) -> dict:
    url = f"{QDRANT_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()}")
        raise


def scroll_all_points():
    """Scroll through all points in the collection."""
    points = []
    offset = None

    while True:
        body: dict = {"limit": BATCH_SIZE, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset

        resp = qdrant_request("POST", f"/collections/{COLLECTION}/points/scroll", body)
        result = resp.get("result", {})
        batch = result.get("points", [])
        points.extend(batch)

        next_offset = result.get("next_page_offset")
        if next_offset is None or len(batch) == 0:
            break
        offset = next_offset
        print(f"  Scrolled {len(points)} points...")

    return points


def derive_source_type(payload: dict) -> str | None:
    """Derive source_type from the 'source' field."""
    source = payload.get("source", "")
    if not source:
        return None

    # source format: "document:inline", "git-commit:inline", etc.
    if ":" in source:
        return source.split(":")[0]

    return None


def set_payload(point_ids: list, source_type: str):
    """Set source_type payload for a batch of points."""
    qdrant_request(
        "POST",
        f"/collections/{COLLECTION}/points/payload",
        {
            "payload": {"source_type": source_type},
            "points": point_ids,
        },
    )


def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"╔══════════════════════════════════════════════╗")
    print(f"║  Backfill source_type — {mode:^20s}  ║")
    print(f"╚══════════════════════════════════════════════╝")
    print(f"\nQdrant:     {QDRANT_URL}")
    print(f"Collection: {COLLECTION}\n")

    # Check collection exists
    try:
        info = qdrant_request("GET", f"/collections/{COLLECTION}")
        count = info.get("result", {}).get("points_count", "?")
        print(f"Points in collection: {count}\n")
    except Exception as e:
        print(f"ERROR: Cannot access collection '{COLLECTION}': {e}")
        return 1

    # Scroll all points
    print("Scrolling all points...")
    points = scroll_all_points()
    print(f"Total points: {len(points)}\n")

    if not points:
        print("No points found — nothing to backfill.")
        return 0

    # Categorize
    by_type: dict[str, list] = {}
    already_set = 0
    no_source = 0

    for p in points:
        payload = p.get("payload", {})
        point_id = p["id"]

        # Skip if already has source_type
        if payload.get("source_type"):
            already_set += 1
            continue

        source_type = derive_source_type(payload)
        if source_type is None:
            no_source += 1
            continue

        by_type.setdefault(source_type, []).append(point_id)

    print(f"Already have source_type: {already_set}")
    print(f"No source field:         {no_source}")
    for st, ids in sorted(by_type.items()):
        print(f"Need backfill ({st}):    {len(ids)}")
    print()

    if not by_type:
        print("Nothing to backfill — all points already have source_type.")
        return 0

    # Apply
    total_updated = 0
    for source_type, point_ids in by_type.items():
        if DRY_RUN:
            print(f"  [DRY RUN] Would set source_type='{source_type}' on {len(point_ids)} points")
            total_updated += len(point_ids)
            continue

        # Batch updates
        for i in range(0, len(point_ids), BATCH_SIZE):
            batch = point_ids[i : i + BATCH_SIZE]
            set_payload(batch, source_type)
            total_updated += len(batch)
            print(f"  ✅ Set source_type='{source_type}' on {len(batch)} points (total: {total_updated})")

    print(f"\n{'Would update' if DRY_RUN else 'Updated'}: {total_updated} points")
    return 0


if __name__ == "__main__":
    exit(main())
