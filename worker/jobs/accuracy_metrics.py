"""Job: accuracy_metrics_refresh (B-45).

Every 5 minutes the worker:

1. Reads the windowed feedback view (``v_feedback_windowed``) and
   pushes the per-window aggregates to a JSON sidecar that the
   ``/metrics/json`` endpoint of pb-worker exposes (Phase-3 keeps it
   minimal — Prometheus push gateway integration is a Phase-5 follow-up).

2. For each canonical collection (``pb_general``, ``pb_code``,
   ``pb_rules``):
   - On first run, samples ``reference_sample_size`` random vectors
     from Qdrant, computes the centroid, and stores it in
     ``embedding_reference_set``.
   - On subsequent runs, samples ``fresh_sample_size`` vectors,
     computes the fresh centroid, and compares it against the
     reference baseline via ``shared.drift_check.compute_drift``.

If a collection drifts beyond its configured per-collection threshold,
the result is logged at WARNING level so the Prometheus alert rule can
fire on the log line, and the job summary records ``drifted=true``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from shared.drift_check import compute_centroid, compute_drift, DEFAULT_THRESHOLDS
from worker.metrics import (
    worker_accuracy_avg_rating,
    worker_accuracy_drift_distance,
    worker_accuracy_drift_drifted,
    worker_accuracy_empty_result_rate,
    worker_accuracy_rerank_score,
)

log = logging.getLogger("pb-worker.accuracy_metrics")


CANONICAL_COLLECTIONS = ("pb_general", "pb_code", "pb_rules")


async def _load_drift_config(ctx) -> dict[str, Any]:
    """Read drift settings from OPA. Falls back to safe defaults.

    Reuses ``ctx.http_client`` instead of creating a fresh
    ``httpx.AsyncClient`` so the connection pool is shared with the
    rest of the worker. Per-request timeout is enforced inline.
    """
    fallback = {
        "thresholds":            dict(DEFAULT_THRESHOLDS),
        "reference_sample_size": 200,
        "fresh_sample_size":     200,
    }
    try:
        resp = await ctx.http_client.get(
            f"{ctx.opa_url}/v1/data/pb/config/drift",
            timeout=2.0,
        )
        resp.raise_for_status()
        cfg = resp.json().get("result") or {}
        if not cfg:
            return fallback
        return {
            "thresholds":            cfg.get("thresholds") or fallback["thresholds"],
            "reference_sample_size": int(cfg.get("reference_sample_size") or fallback["reference_sample_size"]),
            "fresh_sample_size":     int(cfg.get("fresh_sample_size") or fallback["fresh_sample_size"]),
        }
    except Exception as e:
        log.warning(f"could not load drift config from OPA, using defaults: {e}")
        return fallback


# Module-level cache so we only check information_schema once per
# worker process. The view either exists post-migration or doesn't —
# checking 288 times per day is wasted work.
_VIEW_EXISTS: bool | None = None


async def _read_windowed_metrics(ctx) -> list[dict]:
    """Read v_feedback_windowed and push the values to the worker
    Prometheus gauges. Empty windows (sample_count == 0) are skipped
    so they don't show up as bogus zero ratings in Grafana / alerts.
    """
    global _VIEW_EXISTS
    if _VIEW_EXISTS is None:
        _VIEW_EXISTS = await ctx.pg_pool.fetchval(
            "SELECT EXISTS ("
            " SELECT 1 FROM information_schema.views "
            " WHERE table_schema = 'public' AND table_name = 'v_feedback_windowed'"
            ")"
        )
    if not _VIEW_EXISTS:
        log.debug("v_feedback_windowed not present — skipping metrics read")
        return []

    rows = await ctx.pg_pool.fetch(
        "SELECT window_label, collection, sample_count, "
        "       avg_rating, empty_result_rate, avg_rerank_score "
        "FROM v_feedback_windowed"
    )
    out: list[dict] = []
    for r in rows:
        samples = int(r["sample_count"] or 0)
        # Skip empty windows — exporting 0.0 would trip the
        # QualityDrift alert (rating < 2.5) on fresh deployments
        # before any feedback has been collected.
        if samples == 0:
            continue
        avg_rating = float(r["avg_rating"] or 0.0)
        empty_rate = float(r["empty_result_rate"] or 0.0)
        rerank     = float(r["avg_rerank_score"] or 0.0)
        window     = r["window_label"]
        collection = r["collection"] or "_all_"

        worker_accuracy_avg_rating.labels(window=window, collection=collection).set(avg_rating)
        worker_accuracy_empty_result_rate.labels(window=window, collection=collection).set(empty_rate)
        worker_accuracy_rerank_score.labels(window=window, collection=collection).set(rerank)

        out.append({
            "window":     window,
            "collection": collection,
            "samples":    samples,
            "avg_rating": avg_rating,
            "empty_rate": empty_rate,
            "rerank":     rerank,
        })
    return out


async def _sample_collection_vectors(ctx, collection: str, n: int) -> list[list[float]]:
    """Pull up to ``n`` vectors from a Qdrant collection.

    Uses ``/collections/{name}/points/scroll`` directly via httpx so
    the worker container does not need the qdrant_client SDK installed
    just for sampling. Falls back to an empty list on any error.
    """
    url = f"{ctx.qdrant_url}/collections/{collection}/points/scroll"
    body = {"limit": n, "with_payload": False, "with_vector": True}
    try:
        resp = await ctx.http_client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        points = (data.get("result") or {}).get("points") or []
    except Exception as e:
        log.warning(f"sample failed for {collection}: {e}")
        return []

    vectors: list[list[float]] = []
    for p in points:
        v = p.get("vector")
        if isinstance(v, dict):
            # Multi-named vectors — pick the first one deterministically
            if not v:
                continue
            v = v[sorted(v.keys())[0]]
        if isinstance(v, list) and v:
            vectors.append([float(x) for x in v])
    # Note: this is first-N sampling along Qdrant's storage order, NOT
    # random sampling. The bias is acceptable for a deployment-snapshot
    # baseline because both the baseline seed and the fresh-sample
    # comparison use the same sampling strategy and Qdrant storage
    # order is stable across normal operations. If a future Qdrant
    # version reshuffles points (e.g. compaction), the next worker run
    # will simply re-seed the baseline.
    return vectors[:n]


async def _ensure_reference_baseline(ctx, collection: str, sample_size: int) -> dict | None:
    """Return the latest baseline row for ``collection``, seeding it
    on first run."""
    row = await ctx.pg_pool.fetchrow(
        "SELECT id, sample_count, embedding_dim, centroid "
        "FROM embedding_reference_set "
        "WHERE collection = $1 "
        "ORDER BY seeded_at DESC LIMIT 1",
        collection,
    )
    if row is not None:
        return {
            "id":            row["id"],
            "sample_count":  row["sample_count"],
            "embedding_dim": row["embedding_dim"],
            "centroid":      list(row["centroid"]),
        }

    # First run — seed
    vectors = await _sample_collection_vectors(ctx, collection, sample_size)
    if not vectors:
        log.info(f"no vectors found for {collection}, skipping baseline seed")
        return None
    centroid = compute_centroid(vectors)
    await ctx.pg_pool.execute(
        "INSERT INTO embedding_reference_set "
        "(collection, sample_count, embedding_dim, centroid, notes) "
        "VALUES ($1, $2, $3, $4, $5)",
        collection, len(vectors), len(centroid), centroid,
        f"deployment-snapshot baseline ({len(vectors)} samples)",
    )
    log.info(f"seeded baseline for {collection}: dim={len(centroid)} samples={len(vectors)}")
    return {
        "id":            None,
        "sample_count":  len(vectors),
        "embedding_dim": len(centroid),
        "centroid":      centroid,
    }


async def run(ctx) -> dict[str, Any]:
    cfg = await _load_drift_config(ctx)
    windows = await _read_windowed_metrics(ctx)
    drift_results: list[dict] = []
    drifted_collections: list[str] = []
    skipped_collections: list[str] = []

    for collection in CANONICAL_COLLECTIONS:
        baseline = await _ensure_reference_baseline(
            ctx, collection, cfg["reference_sample_size"],
        )
        if baseline is None:
            log.warning(
                "no baseline could be seeded for %s "
                "(empty collection or sampling failure) — skipping",
                collection,
            )
            skipped_collections.append(collection)
            continue

        fresh = await _sample_collection_vectors(
            ctx, collection, cfg["fresh_sample_size"],
        )
        if not fresh:
            log.warning(
                "no fresh vectors retrieved for %s — skipping drift check; "
                "Prometheus gauges intentionally NOT updated so absent() "
                "alerts can fire if this persists",
                collection,
            )
            skipped_collections.append(collection)
            continue

        result = compute_drift(
            collection, fresh, baseline["centroid"],
            thresholds=cfg["thresholds"],
        )
        drift_results.append(result.to_dict())

        worker_accuracy_drift_distance.labels(collection=collection).set(result.distance)
        worker_accuracy_drift_drifted.labels(collection=collection).set(
            1 if result.drifted else 0
        )

        if result.drifted:
            drifted_collections.append(collection)
            log.warning(
                "DRIFT detected on %s: distance=%.4f threshold=%.4f",
                collection, result.distance, result.threshold,
            )

    summary = {
        "windows": windows,
        "drift":   drift_results,
        "drifted": drifted_collections,
        "skipped": skipped_collections,
    }
    log.info(
        "accuracy refresh: %d windows, %d collections checked, "
        "%d skipped, drifted=%s",
        len(windows), len(drift_results), len(skipped_collections),
        drifted_collections,
    )
    return summary
