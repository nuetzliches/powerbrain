"""Tests for the Microsoft Graph API client."""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from ingestion.adapters.office365.graph_client import (
    GRAPH_BASE,
    GraphClient,
    GraphClientConfig,
    RateLimitBudget,
    TokenCache,
)

# ── Token Cache ─────────────────────────────────────────────


class TestTokenCache:
    def test_empty_cache_not_valid(self):
        cache = TokenCache()
        assert not cache.valid

    def test_valid_token(self):
        cache = TokenCache(access_token="tok", expires_at=time.time() + 3600)
        assert cache.valid

    def test_expired_token(self):
        cache = TokenCache(access_token="tok", expires_at=time.time() - 10)
        assert not cache.valid

    def test_near_expiry_not_valid(self):
        # Within 300s buffer → not valid
        cache = TokenCache(access_token="tok", expires_at=time.time() + 200)
        assert not cache.valid


# ── Rate Limit Budget ───────────────────────────────────────


class TestRateLimitBudget:
    def test_fresh_budget_no_throttle(self):
        budget = RateLimitBudget(max_units_per_minute=1000)
        assert not budget.should_throttle()

    def test_record_units(self):
        budget = RateLimitBudget(max_units_per_minute=100)
        budget.window_start = time.monotonic()
        budget.record(50)
        assert budget.units_used == 50
        assert not budget.should_throttle()

    def test_throttle_at_80_percent(self):
        budget = RateLimitBudget(max_units_per_minute=100)
        budget.window_start = time.monotonic()
        budget.units_used = 80
        assert budget.should_throttle()

    def test_window_resets_after_60s(self):
        budget = RateLimitBudget(max_units_per_minute=100)
        budget.window_start = time.monotonic() - 61
        budget.units_used = 200
        assert not budget.should_throttle()

    def test_wait_seconds(self):
        budget = RateLimitBudget(max_units_per_minute=100)
        budget.window_start = time.monotonic()
        budget.units_used = 80
        wait = budget.wait_seconds()
        assert 50 < wait <= 60


# ── Graph Client Auth ───────────────────────────────────────


@pytest.fixture
def graph_config():
    return GraphClientConfig(
        tenant_id="test-tenant",
        client_id="test-client",
        client_secret="test-secret",
    )


@pytest.fixture
def graph_client(graph_config):
    http = httpx.AsyncClient()
    return GraphClient(graph_config, http)


@respx.mock
@pytest.mark.asyncio
async def test_acquire_app_token(graph_client):
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "test_app_token",
            "expires_in": 3600,
        })
    )

    token = await graph_client._acquire_app_token()
    assert token == "test_app_token"
    # Second call should use cache
    token2 = await graph_client._acquire_app_token()
    assert token2 == "test_app_token"


@respx.mock
@pytest.mark.asyncio
async def test_acquire_delegated_token(graph_config):
    graph_config.refresh_token = "test_refresh"
    http = httpx.AsyncClient()
    client = GraphClient(graph_config, http)

    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "test_delegated_token",
            "expires_in": 3600,
            "refresh_token": "new_refresh_token",
        })
    )

    token = await client._acquire_delegated_token()
    assert token == "test_delegated_token"
    # Refresh token should be rotated
    assert client.config.refresh_token == "new_refresh_token"


@pytest.mark.asyncio
async def test_delegated_without_refresh_raises(graph_client):
    with pytest.raises(ValueError, match="refresh_token"):
        await graph_client._acquire_delegated_token()


# ── Graph Client Requests ───────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_request(graph_client):
    # Mock token
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )
    # Mock API call
    respx.get(f"{GRAPH_BASE}/me").mock(
        return_value=httpx.Response(200, json={"displayName": "Test User"})
    )

    data = await graph_client.get("/me")
    assert data["displayName"] == "Test User"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429(graph_client):
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )

    route = respx.get(f"{GRAPH_BASE}/test")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"ok": True}),
    ]

    data = await graph_client.get("/test")
    assert data["ok"] is True
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500(graph_client):
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )

    route = respx.get(f"{GRAPH_BASE}/test")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(200, json={"ok": True}),
    ]

    data = await graph_client.get("/test")
    assert data["ok"] is True


# ── Pagination ──────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_all_pages(graph_client):
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )

    next_url = f"{GRAPH_BASE}/items?$skiptoken=abc"

    # Use side_effect on a single route to control pagination
    route = respx.get(url__startswith=f"{GRAPH_BASE}/items")
    route.side_effect = [
        httpx.Response(200, json={
            "value": [{"id": "1"}, {"id": "2"}],
            "@odata.nextLink": next_url,
        }),
        httpx.Response(200, json={
            "value": [{"id": "3"}],
        }),
    ]

    items = await graph_client.get_all_pages("/items")
    assert len(items) == 3
    assert items[2]["id"] == "3"


# ── Delta Queries ───────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_delta_query_initial(graph_client):
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )

    respx.get(f"{GRAPH_BASE}/drives/d1/root/delta").mock(
        return_value=httpx.Response(200, json={
            "value": [{"id": "item1"}, {"id": "item2"}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/d1/root/delta?token=xyz",
        })
    )

    items, delta_link = await graph_client.delta_query("/drives/d1/root/delta")
    assert len(items) == 2
    assert "token=xyz" in delta_link


@respx.mock
@pytest.mark.asyncio
async def test_delta_query_incremental(graph_client):
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )

    delta_url = "https://graph.microsoft.com/v1.0/drives/d1/root/delta?token=old"
    respx.get(delta_url).mock(
        return_value=httpx.Response(200, json={
            "value": [{"id": "changed_item"}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/d1/root/delta?token=new",
        })
    )

    items, new_link = await graph_client.delta_query(
        "/drives/d1/root/delta", delta_link=delta_url,
    )
    assert len(items) == 1
    assert "token=new" in new_link


# ── Batch Requests ──────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_batch_request(graph_client):
    respx.post(
        "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "expires_in": 3600,
        })
    )

    respx.post(f"{GRAPH_BASE}/$batch").mock(
        return_value=httpx.Response(200, json={
            "responses": [
                {"id": "1", "status": 200, "body": {"name": "file1"}},
                {"id": "2", "status": 200, "body": {"name": "file2"}},
            ]
        })
    )

    responses = await graph_client.batch([
        {"id": "1", "method": "GET", "url": "/drives/d1/items/a"},
        {"id": "2", "method": "GET", "url": "/drives/d1/items/b"},
    ])
    assert len(responses) == 2
    assert responses[0]["body"]["name"] == "file1"


@pytest.mark.asyncio
async def test_batch_over_limit_raises(graph_client):
    with pytest.raises(ValueError, match="Batch limit"):
        await graph_client.batch([{"id": str(i)} for i in range(21)])
