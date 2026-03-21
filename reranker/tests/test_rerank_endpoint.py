"""Tests for reranker /rerank endpoint with mocked CrossEncoder model."""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

import service
from service import app


@pytest.fixture(autouse=True)
def _set_model(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(service, "model", mock)
    return mock


@pytest.fixture
def client():
    return TestClient(app)


class TestRerankEndpoint:
    def test_basic_reranking(self, client, _set_model):
        _set_model.predict.return_value = [0.9, 0.1, 0.5]

        resp = client.post("/rerank", json={
            "query": "test query",
            "documents": [
                {"id": "a", "content": "Doc A"},
                {"id": "b", "content": "Doc B"},
                {"id": "c", "content": "Doc C"},
            ],
            "top_n": 2,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["output_count"] == 2
        assert data["results"][0]["id"] == "a"
        assert data["results"][0]["rank"] == 1

    def test_empty_documents(self, client):
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [],
            "top_n": 5,
        })
        assert resp.status_code == 200
        assert resp.json()["output_count"] == 0

    def test_model_not_loaded_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(service, "model", None)
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [{"id": "a", "content": "x"}],
        })
        assert resp.status_code == 503

    def test_batch_too_large_returns_400(self, client, _set_model, monkeypatch):
        monkeypatch.setattr(service, "MAX_BATCH_SIZE", 2)
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [
                {"id": str(i), "content": f"doc {i}"} for i in range(5)
            ],
        })
        assert resp.status_code == 400

    def test_scores_in_response(self, client, _set_model):
        _set_model.predict.return_value = [0.8]
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [{"id": "a", "content": "Doc A", "score": 0.5}],
            "top_n": 1,
        })
        data = resp.json()
        assert data["results"][0]["original_score"] == 0.5
        assert data["results"][0]["rerank_score"] == 0.8

    def test_top_n_capped_to_document_count(self, client, _set_model):
        _set_model.predict.return_value = [0.7, 0.3]
        resp = client.post("/rerank", json={
            "query": "test",
            "documents": [
                {"id": "a", "content": "Doc A"},
                {"id": "b", "content": "Doc B"},
            ],
            "top_n": 10,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["output_count"] == 2
        assert data["input_count"] == 2

    def test_response_includes_model_name(self, client, _set_model):
        _set_model.predict.return_value = [0.5]
        resp = client.post("/rerank", json={
            "query": "q",
            "documents": [{"id": "a", "content": "c"}],
            "top_n": 1,
        })
        data = resp.json()
        assert data["model"] == service.MODEL_NAME
        assert data["query"] == "q"

    def test_predict_called_with_pairs(self, client, _set_model):
        _set_model.predict.return_value = [0.5, 0.3]
        client.post("/rerank", json={
            "query": "my query",
            "documents": [
                {"id": "a", "content": "text A"},
                {"id": "b", "content": "text B"},
            ],
            "top_n": 2,
        })
        call_args = _set_model.predict.call_args
        pairs = call_args[0][0]
        assert pairs == [["my query", "text A"], ["my query", "text B"]]
        assert call_args[1]["show_progress_bar"] is False
