"""Powerbrain Maintenance Worker — APScheduler entry point.

Runs four periodic jobs in a single process:

- accuracy_metrics_refresh   every 5 minutes  (B-45 placeholder)
- pending_review_timeout     every hour       (B-42)
- gdpr_retention_cleanup     daily 02:00      (existing)
- audit_retention_cleanup    daily 03:00      (B-40)

Designed to be deployable ahead of B-45: jobs are independent and the
accuracy_metrics job no-ops gracefully when its SQL view does not yet
exist. Run via ``python -m worker.scheduler``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from prometheus_client import start_http_server

from worker.context import WorkerContext
from worker.jobs import (
    accuracy_metrics,
    audit_integrity_status,
    audit_retention,
    gdpr_retention,
    pending_review_timeout,
    repo_sync,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("pb-worker")


JobFunc = Callable[[WorkerContext], Awaitable[Any]]


# Job specs are exposed at module level so tests can introspect them
# without spinning up the scheduler.
JOB_SPECS: list[dict] = [
    {
        "id":      "accuracy_metrics_refresh",
        "func":    accuracy_metrics.run,
        "trigger": IntervalTrigger(minutes=5, jitter=15),
    },
    {
        "id":      "pending_review_timeout",
        "func":    pending_review_timeout.run,
        "trigger": IntervalTrigger(hours=1, jitter=60),
    },
    {
        "id":      "gdpr_retention_cleanup",
        "func":    gdpr_retention.run,
        "trigger": CronTrigger(hour=2, minute=0),
    },
    {
        "id":      "audit_retention_cleanup",
        "func":    audit_retention.run,
        "trigger": CronTrigger(hour=3, minute=0),
    },
    {
        "id":      "repo_sync",
        "func":    repo_sync.run,
        "trigger": IntervalTrigger(
            minutes=int(os.getenv("REPO_SYNC_INTERVAL_MINUTES", "5")),
            jitter=30,
        ),
    },
    {
        "id":      "audit_integrity_status_refresh",
        "func":    audit_integrity_status.run,
        "trigger": IntervalTrigger(
            seconds=int(os.getenv("AUDIT_INTEGRITY_INTERVAL_SECONDS", "60")),
            jitter=10,
        ),
    },
]


async def _run_with_logging(name: str, func: JobFunc, ctx: WorkerContext) -> None:
    """Wrap a job to capture exceptions and log structured outcomes."""
    log.info("job start: %s", name)
    try:
        result = await func(ctx)
        log.info("job ok:    %s → %s", name, result)
    except Exception as e:
        log.exception("job fail:  %s: %s", name, e)


def register_jobs(scheduler: AsyncIOScheduler, ctx: WorkerContext) -> None:
    """Attach all configured jobs to the scheduler. Pure function so
    tests can introspect the registration without running anything."""
    for spec in JOB_SPECS:
        scheduler.add_job(
            _run_with_logging,
            trigger=spec["trigger"],
            id=spec["id"],
            args=(spec["id"], spec["func"], ctx),
            coalesce=True,
            max_instances=1,
        )
        log.info("registered job: %s (%s)", spec["id"], spec["trigger"])


async def main() -> None:
    log.info("pb-worker starting")
    ctx = await WorkerContext.create()
    log.info("postgres connected (%s)", os.getenv("POSTGRES_HOST", "postgres"))

    # Import worker.metrics for the side-effect of registering the
    # gauges before the HTTP server starts so /metrics never lies.
    from worker import metrics  # noqa: F401

    metrics_port = int(os.getenv("WORKER_METRICS_PORT", "8083"))
    start_http_server(metrics_port)
    log.info("prometheus metrics endpoint listening on :%d", metrics_port)

    scheduler = AsyncIOScheduler()
    register_jobs(scheduler, ctx)
    scheduler.start()
    log.info("scheduler started with %s jobs", len(JOB_SPECS))

    stop_event = asyncio.Event()

    def _on_signal(*_args):
        log.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        await ctx.close()
        log.info("pb-worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
