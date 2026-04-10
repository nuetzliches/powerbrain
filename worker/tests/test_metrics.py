"""Tests for worker.metrics — Prometheus gauge declarations."""

from prometheus_client import Gauge

from worker import metrics


EXPECTED_GAUGES = [
    "worker_accuracy_avg_rating",
    "worker_accuracy_empty_result_rate",
    "worker_accuracy_rerank_score",
    "worker_accuracy_drift_distance",
    "worker_accuracy_drift_drifted",
]


class TestMetricsDeclarations:
    def test_all_gauges_registered(self):
        for name in EXPECTED_GAUGES:
            obj = getattr(metrics, name)
            assert isinstance(obj, Gauge), f"{name} is not a Gauge"

    def test_gauge_labels(self):
        windowed = [
            metrics.worker_accuracy_avg_rating,
            metrics.worker_accuracy_empty_result_rate,
            metrics.worker_accuracy_rerank_score,
        ]
        for g in windowed:
            assert g._labelnames == ("window", "collection")

        drift = [
            metrics.worker_accuracy_drift_distance,
            metrics.worker_accuracy_drift_drifted,
        ]
        for g in drift:
            assert g._labelnames == ("collection",)

    def test_gauge_names_have_pb_prefix(self):
        for attr_name in EXPECTED_GAUGES:
            g = getattr(metrics, attr_name)
            assert g._name.startswith("pb_accuracy_"), \
                f"{attr_name} has unexpected metric name: {g._name}"
