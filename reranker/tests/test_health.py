"""Tests for reranker /health and /models endpoints."""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

import service
from service import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealthEndpoint:
    def test_health_ok_when_model_loaded(self, client, monkeypatch):
        monkeypatch.setattr(service, "model", MagicMock())
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_loading_when_no_model(self, client, monkeypatch):
        monkeypatch.setattr(service, "model", None)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "loading"

    def test_health_includes_model_name(self, client, monkeypatch):
        monkeypatch.setattr(service, "model", MagicMock())
        resp = client.get("/health")
        data = resp.json()
        assert data["model"] == service.MODEL_NAME


class TestModelsEndpoint:
    def test_lists_model_info(self, client):
        resp = client.get("/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "current" in data
        assert "alternatives" in data
        assert "max_batch_size" in data

    def test_current_matches_config(self, client):
        resp = client.get("/models")
        data = resp.json()
        assert data["current"] == service.MODEL_NAME
        assert data["max_batch_size"] == service.MAX_BATCH_SIZE

    def test_alternatives_contain_expected_keys(self, client):
        resp = client.get("/models")
        alts = resp.json()["alternatives"]
        assert "fast" in alts
        assert "accurate" in alts
        assert "multilingual" in alts
