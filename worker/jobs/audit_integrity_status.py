"""Job: audit_integrity_status_refresh (#95).

Runs pb_verify_audit_chain_tail() periodically and UPSERTs the result
into audit_integrity_status (single-row cache). The mcp-server reads
that cache for its transparency snapshot, decoupling the
audit_integrity field from the request path.

On any error, the cache row's `error` column is updated so consumers
of the snapshot can see what went wrong; the exception is re-raised
so the scheduler logs and metrics reflect the failure.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("pb-worker.audit_integrity_status")


async def run(ctx) -> dict[str, Any]:
    tail_rows = ctx.audit_status_tail_rows
    try:
        row = await ctx.pg_pool.fetchrow(
            "SELECT valid, first_invalid_id, total_checked, last_valid_hash "
            "FROM pb_verify_audit_chain_tail($1)",
            tail_rows,
        )
        await ctx.pg_pool.execute(
            """
            INSERT INTO audit_integrity_status (
                id, valid, total_checked, first_invalid_id, last_valid_hash,
                checked_at, error, updated_at
            )
            VALUES (1, $1, $2, $3, $4, now(), NULL, now())
            ON CONFLICT (id) DO UPDATE SET
                valid            = EXCLUDED.valid,
                total_checked    = EXCLUDED.total_checked,
                first_invalid_id = EXCLUDED.first_invalid_id,
                last_valid_hash  = EXCLUDED.last_valid_hash,
                checked_at       = EXCLUDED.checked_at,
                error            = NULL,
                updated_at       = now()
            """,
            row["valid"],
            row["total_checked"],
            row["first_invalid_id"],
            row["last_valid_hash"],
        )
        summary = {
            "valid":            row["valid"],
            "total_checked":    row["total_checked"],
            "first_invalid_id": row["first_invalid_id"],
            "tail_rows":        tail_rows,
        }
        if not row["valid"]:
            log.error("audit chain invalid! cached: %s", summary)
        else:
            log.info("audit_integrity_status refresh: %s", summary)
        return summary
    except Exception as e:
        log.exception("audit_integrity_status refresh failed: %s", e)
        try:
            await ctx.pg_pool.execute(
                """
                INSERT INTO audit_integrity_status (id, error, updated_at)
                VALUES (1, $1, now())
                ON CONFLICT (id) DO UPDATE SET
                    error      = EXCLUDED.error,
                    updated_at = now()
                """,
                str(e)[:500],
            )
        except Exception as e2:
            log.error("could not persist audit_integrity_status error: %s", e2)
        raise
