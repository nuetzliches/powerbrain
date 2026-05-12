"""Job: incident_deadline_check (B-47).

Watches the GDPR Art. 33 72-hour notification deadline. Queries the
``v_incidents_requiring_attention`` view, classifies each open incident
into a severity bucket (warning / critical / overdue) and exposes the
counts as Prometheus gauges so the ``IncidentNotificationDeadlineImminent``
and ``IncidentNotificationOverdue`` alerts can fire.

Severity buckets:

- ``warning``  — ``status='detected'`` and ``hours_since_detection > warning_threshold_hours``
  (default 24h). The DPO has not yet started the assessment.
- ``critical`` — ``hours_since_detection > critical_threshold_hours`` (default 48h)
  and status is not yet ``notified_authority``. Less than 24h before the
  72-hour deadline.
- ``overdue``  — ``hours_since_detection > notification_hours`` (default 72h)
  and status is not yet ``notified_authority``. The deadline has passed
  without notification; the DPO must document a delay reason (Art. 33(1)
  sentence 2).

Powerbrain does not send the notification itself — this job only makes
sure the deadline cannot silently slip past operations.
"""

from __future__ import annotations

import logging
from typing import Any

from worker.metrics import (
    worker_incidents_attention,
    worker_incidents_oldest_open_hours,
    worker_incidents_open,
)

log = logging.getLogger("pb-worker.incident_deadline_check")


_OPEN_STATUSES = {
    "detected", "under_review", "contained", "notified_subject",
}


async def _load_thresholds(ctx) -> tuple[int, int, int]:
    """Pull deadline configuration from OPA. Fail-open to GDPR defaults."""
    defaults = (72, 24, 48)
    try:
        resp = await ctx.http_client.post(
            f"{ctx.opa_url}/v1/data/pb/config/incidents/deadline",
            json={"input": {}},
            timeout=2.0,
        )
        resp.raise_for_status()
        data = resp.json().get("result") or {}
        return (
            int(data.get("notification_hours") or defaults[0]),
            int(data.get("warning_threshold_hours") or defaults[1]),
            int(data.get("critical_threshold_hours") or defaults[2]),
        )
    except Exception as e:
        log.warning("could not load incident deadline thresholds from OPA "
                    "(%s) — falling back to GDPR defaults %s", e, defaults)
        return defaults


async def run(ctx) -> dict[str, Any]:
    notification_hours, warning_hours, critical_hours = await _load_thresholds(ctx)

    # ── Per-status open counts ───────────────────────────────────
    status_rows = await ctx.pg_pool.fetch(
        """
        SELECT status::text AS status, COUNT(*) AS n
        FROM privacy_incidents
        WHERE status NOT IN ('resolved', 'false_positive', 'notified_authority')
        GROUP BY status
        """
    )
    status_counts = {r["status"]: int(r["n"]) for r in status_rows}
    # Reset all known statuses to 0 first so the gauge reflects the
    # absence of incidents in a bucket between scrapes.
    for s in _OPEN_STATUSES:
        worker_incidents_open.labels(status=s).set(status_counts.get(s, 0))
    # Anything not in the canonical set still gets reported.
    for status, n in status_counts.items():
        if status not in _OPEN_STATUSES:
            worker_incidents_open.labels(status=status).set(n)

    # ── Deadline buckets ────────────────────────────────────────
    rows = await ctx.pg_pool.fetch(
        """
        SELECT id::text,
               status::text AS status,
               EXTRACT(EPOCH FROM (now() - detected_at)) / 3600.0 AS hours_since
        FROM privacy_incidents
        WHERE status NOT IN ('resolved', 'false_positive', 'notified_authority')
        """
    )

    counts = {"warning": 0, "critical": 0, "overdue": 0}
    oldest_hours = 0.0
    overdue_ids: list[str] = []
    critical_ids: list[str] = []

    for r in rows:
        hours = float(r["hours_since"] or 0)
        if hours > oldest_hours:
            oldest_hours = hours
        if hours > notification_hours:
            counts["overdue"] += 1
            overdue_ids.append(r["id"])
        elif hours > critical_hours:
            counts["critical"] += 1
            critical_ids.append(r["id"])
        elif hours > warning_hours and r["status"] == "detected":
            counts["warning"] += 1

    for severity, n in counts.items():
        worker_incidents_attention.labels(severity=severity).set(n)
    worker_incidents_oldest_open_hours.set(oldest_hours)

    summary = {
        "open_by_status":       status_counts,
        "deadline_buckets":     counts,
        "oldest_open_hours":    round(oldest_hours, 2),
        "thresholds_hours": {
            "notification": notification_hours,
            "warning":      warning_hours,
            "critical":     critical_hours,
        },
    }

    if counts["overdue"]:
        log.error(
            "GDPR Art. 33 deadline EXCEEDED for %s incident(s) — DPO must "
            "document delay reason. ids=%s",
            counts["overdue"], overdue_ids[:10],
        )
    elif counts["critical"]:
        log.warning(
            "GDPR Art. 33 deadline within 24h for %s incident(s). ids=%s",
            counts["critical"], critical_ids[:10],
        )
    elif counts["warning"] or any(status_counts.values()):
        log.info("incident_deadline_check: %s", summary)
    else:
        log.debug("incident_deadline_check: no open incidents")

    return summary
