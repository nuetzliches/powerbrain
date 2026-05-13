"""Prometheus metrics owned by pb-worker.

The worker runs in a separate process from mcp-server, so it cannot
mutate any Gauge objects defined in the server. Instead, the worker
registers its own metrics here and exposes them on its own
``/metrics`` HTTP endpoint via prometheus_client's ``start_http_server``.
Prometheus is configured (in ``monitoring/prometheus.yml``) to scrape
both the mcp-server and the worker.

All gauges related to EU AI Act Art. 15 accuracy monitoring (B-45)
live here. Adding a new gauge requires:
1. Declare it below.
2. Set it from the corresponding worker job.
3. Reference it in alerting_rules.yml and the Grafana dashboard.
"""

from __future__ import annotations

from prometheus_client import Gauge


# ── B-45 Accuracy Monitoring ─────────────────────────────────

worker_accuracy_avg_rating = Gauge(
    "pb_accuracy_avg_rating",
    "Average feedback rating (windowed)",
    ["window", "collection"],
)
worker_accuracy_empty_result_rate = Gauge(
    "pb_accuracy_empty_result_rate",
    "Fraction of feedback with empty result_ids (windowed)",
    ["window", "collection"],
)
worker_accuracy_rerank_score = Gauge(
    "pb_accuracy_rerank_score",
    "Average reranker score across feedback rows (windowed)",
    ["window", "collection"],
)
worker_accuracy_drift_distance = Gauge(
    "pb_accuracy_drift_distance",
    "Cosine distance between fresh-document centroid and reference baseline",
    ["collection"],
)
worker_accuracy_drift_drifted = Gauge(
    "pb_accuracy_drift_drifted",
    "1 when the collection has drifted past its threshold, 0 otherwise",
    ["collection"],
)


# ── B-47 Privacy Incident Deadline Monitoring (GDPR Art. 33) ─

worker_incidents_open = Gauge(
    "pb_incidents_open_total",
    "Open privacy incidents by status (status not in {resolved, false_positive, notified_authority})",
    ["status"],
)
worker_incidents_attention = Gauge(
    "pb_incidents_attention_total",
    "Open privacy incidents grouped by deadline severity",
    ["severity"],
)
worker_incidents_oldest_open_hours = Gauge(
    "pb_incidents_oldest_open_hours",
    "Hours since detection for the oldest open privacy incident "
    "(0 when nothing is open)",
)
