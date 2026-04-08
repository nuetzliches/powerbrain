"""Job: audit_retention_cleanup (B-40).

Calls pb_audit_checkpoint_and_prune() with the configured retention
window. The SQL function verifies the chain, writes a checkpoint to
audit_archive, and prunes the rows. If the chain is broken the rows
are NOT deleted (fail-closed).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("pb-worker.audit_retention")


async def run(ctx) -> dict[str, Any]:
    days = ctx.audit_retention_days
    if days < 1:
        log.warning("audit_retention_days < 1, skipping cleanup")
        return {"skipped": True, "reason": "retention_days_lt_1"}

    row = await ctx.pg_pool.fetchrow(
        "SELECT * FROM pb_audit_checkpoint_and_prune($1)", days,
    )
    summary = {
        "checkpoint_id":    row["checkpoint_id"],
        "last_entry_id":    row["last_entry_id"],
        "row_count":        row["row_count"],
        "deleted_count":    row["deleted_count"],
        "chain_valid":      row["chain_valid"],
        "first_invalid_id": row["first_invalid_id"],
        "retention_days":   days,
    }
    if not row["chain_valid"]:
        log.error("audit chain invalid! checkpoint written, no rows pruned: %s", summary)
    else:
        log.info("audit retention cleanup: %s", summary)
    return summary
