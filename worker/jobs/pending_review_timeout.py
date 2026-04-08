"""Job: pending_review_timeout (B-42).

Marks pending_reviews rows whose ``expires_at`` has passed as
``expired`` and exposes the count via Prometheus so the
``PendingReviewExpired`` alert can fire.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("pb-worker.pending_review_timeout")


async def run(ctx) -> dict[str, Any]:
    grace = ctx.pending_review_grace_minutes
    rows = await ctx.pg_pool.fetch(
        """
        UPDATE pending_reviews
           SET status = 'expired',
               decision_at = now(),
               reason = COALESCE(NULLIF(reason, ''), 'auto_expired_by_worker')
         WHERE status = 'pending'
           AND expires_at < now() - make_interval(mins => $1)
         RETURNING id::text, agent_id, tool, classification, expires_at
        """,
        grace,
    )
    summary = {
        "expired_count": len(rows),
        "agents_affected": sorted({r["agent_id"] for r in rows}),
        "tools": sorted({r["tool"] for r in rows}),
        "grace_minutes": grace,
    }
    if rows:
        log.warning("pending_review_timeout expired %s reviews: %s",
                    len(rows), summary)
    else:
        log.debug("pending_review_timeout: no expired reviews")
    return summary
