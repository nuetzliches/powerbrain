"""Job: gdpr_retention_cleanup.

Thin adapter around the existing ``ingestion/retention_cleanup.py``
script. The CLI script remains usable for manual dry-runs; the worker
imports its high-level helpers and runs them on the schedule.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("pb-worker.gdpr_retention")


async def run(ctx) -> dict[str, Any]:
    try:
        # Late import so the worker container does not need spaCy etc.
        # The script is invoked through subprocess to keep its module
        # state isolated and to reuse its proven CLI behaviour.
        import asyncio, os, sys
        env = os.environ.copy()
        env["PYTHONPATH"] = "/app:/app/ingestion:/app/shared"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ingestion.retention_cleanup", "--execute",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return {
            "exit_code": proc.returncode,
            "stdout":    (stdout.decode("utf-8", errors="replace")[-2000:]
                          if stdout else ""),
            "stderr":    (stderr.decode("utf-8", errors="replace")[-2000:]
                          if stderr else ""),
        }
    except FileNotFoundError as e:
        log.warning("gdpr_retention_cleanup: ingestion module not available: %s", e)
        return {"skipped": True, "reason": "ingestion_module_unavailable"}
    except Exception as e:
        log.error("gdpr_retention_cleanup failed: %s", e)
        return {"error": str(e)[:500]}
