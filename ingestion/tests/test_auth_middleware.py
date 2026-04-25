"""Tests for IngestionAuthMiddleware (B-50).

Boots the ingestion FastAPI app via TestClient with the middleware
explicitly enabled (the production module reads the token from env at
import-time; here we re-attach the middleware with a known token to
exercise both the 401 path and the back-compat allow-all behaviour).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth_middleware import IngestionAuthMiddleware  # noqa: E402


@pytest.fixture
def protected_app():
    app = FastAPI()

    @app.post("/scan")
    async def scan_endpoint():
        return {"ok": True}

    @app.get("/health")
    async def health_endpoint():
        return {"status": "ok"}

    @app.get("/metrics/json")
    async def metrics_json_endpoint():
        return {"service": "ingestion"}

    return app


class TestEnforced:
    """Token configured → middleware rejects unauthenticated calls."""

    def _client(self, app: FastAPI, token: str = "secret-token") -> TestClient:
        app.add_middleware(IngestionAuthMiddleware, expected_token=token)
        return TestClient(app)

    def test_no_auth_header_rejected(self, protected_app):
        client = self._client(protected_app)
        resp = client.post("/scan", json={})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Authentication required"
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_wrong_scheme_rejected(self, protected_app):
        client = self._client(protected_app)
        resp = client.post(
            "/scan", json={}, headers={"Authorization": "Basic xyz"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Authentication required"

    def test_wrong_token_rejected(self, protected_app):
        client = self._client(protected_app)
        resp = client.post(
            "/scan", json={}, headers={"Authorization": "Bearer nope"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid service token"

    def test_correct_token_allowed(self, protected_app):
        client = self._client(protected_app)
        resp = client.post(
            "/scan", json={}, headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_health_path_exempt(self, protected_app):
        client = self._client(protected_app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_metrics_json_path_exempt(self, protected_app):
        client = self._client(protected_app)
        resp = client.get("/metrics/json")
        assert resp.status_code == 200

    def test_metrics_subpath_exempt(self, protected_app):
        # Prometheus mounts /metrics with multiple subpaths; the
        # middleware's startswith('/metrics/') guard keeps them all
        # accessible.
        @protected_app.get("/metrics/extra")
        async def metrics_extra():
            return {"x": 1}

        client = self._client(protected_app)
        resp = client.get("/metrics/extra")
        assert resp.status_code == 200


class TestBackCompatNoToken:
    """Token NOT configured → loud warning + allow everything."""

    def test_warns_at_init(self, caplog):
        # The middleware logs the warning in __init__; instantiate it
        # directly so the test doesn't depend on Starlette's lazy
        # middleware construction.
        with caplog.at_level("WARNING", logger="pb-ingestion.auth"):
            IngestionAuthMiddleware(app=lambda *a, **kw: None, expected_token="")
        joined = " ".join(rec.message for rec in caplog.records)
        assert "INGESTION_AUTH_TOKEN" in joined
        assert "unauthenticated" in joined

    def test_allows_unauthenticated_request(self, protected_app):
        protected_app.add_middleware(
            IngestionAuthMiddleware, expected_token=""
        )
        client = TestClient(protected_app)
        resp = client.post("/scan", json={})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
