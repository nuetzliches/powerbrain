"""Microsoft Graph API client with OAuth2 auth, rate-limit tracking, and $batch support.

Handles two auth modes:
- Client Credentials (app-only) for SharePoint, OneDrive, Outlook, Teams
- Authorization Code (delegated) for OneNote (app-only deprecated March 2025)

SharePoint uses a Resource Unit model, not simple request counts.
See: https://learn.microsoft.com/en-us/sharepoint/dev/general-development/
     how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("pb-graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"
AUTH_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

# Default scope for client credentials (app-only)
DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# User-Agent for priority traffic (Microsoft recommendation)
USER_AGENT = "ISV|Powerbrain|Office365Adapter/1.0"

# Resource unit costs for SharePoint operations
RU_COSTS = {
    "get_item": 1,
    "delta": 1,  # discounted from 2
    "download": 1,
    "list": 2,
    "mutate": 2,
    "permissions": 5,
}

MAX_BATCH_SIZE = 20  # Graph $batch limit
MAX_RETRIES = 5
BACKOFF_BASE = 2.0


@dataclass
class RateLimitBudget:
    """Track resource unit consumption per minute window."""

    window_start: float = 0.0
    units_used: int = 0
    max_units_per_minute: int = 1250  # smallest tenant tier

    def record(self, units: int) -> None:
        now = time.monotonic()
        if now - self.window_start > 60:
            self.window_start = now
            self.units_used = 0
        self.units_used += units

    def should_throttle(self) -> bool:
        now = time.monotonic()
        if now - self.window_start > 60:
            return False
        return self.units_used >= int(self.max_units_per_minute * 0.8)

    def wait_seconds(self) -> float:
        if not self.should_throttle():
            return 0.0
        elapsed = time.monotonic() - self.window_start
        return max(0.0, 60 - elapsed)


@dataclass
class TokenCache:
    """Cached OAuth2 token with expiry tracking."""

    access_token: str = ""
    expires_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - 300


@dataclass
class GraphClientConfig:
    """Configuration for a Microsoft Graph client."""

    tenant_id: str
    client_id: str
    client_secret: str
    # For delegated auth (OneNote)
    refresh_token: str | None = None
    # Resource unit budget per minute (depends on tenant size)
    ru_budget_per_minute: int = 1250


class GraphClient:
    """Microsoft Graph API client with auth, rate limiting, and batching."""

    def __init__(self, config: GraphClientConfig, http_client: httpx.AsyncClient):
        self.config = config
        self.http = http_client
        self._app_token = TokenCache()
        self._delegated_token = TokenCache()
        self._budget = RateLimitBudget(max_units_per_minute=config.ru_budget_per_minute)

    # ── Authentication ──────────────────────────────────────────

    async def _acquire_app_token(self) -> str:
        """Acquire token via Client Credentials flow (app-only)."""
        if self._app_token.valid:
            return self._app_token.access_token

        url = AUTH_URL.format(tenant_id=self.config.tenant_id)
        resp = await self.http.post(
            url,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": DEFAULT_SCOPE,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        self._app_token.access_token = data["access_token"]
        self._app_token.expires_at = time.time() + data.get("expires_in", 3600)
        log.debug("Acquired app token, expires in %ds", data.get("expires_in", 3600))
        return self._app_token.access_token

    async def _acquire_delegated_token(self) -> str:
        """Acquire token via Refresh Token flow (delegated, for OneNote)."""
        if self._delegated_token.valid:
            return self._delegated_token.access_token

        if not self.config.refresh_token:
            raise ValueError(
                "Delegated auth requires a refresh_token. "
                "OneNote API does not support app-only permissions since March 2025."
            )

        url = AUTH_URL.format(tenant_id=self.config.tenant_id)
        resp = await self.http.post(
            url,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": self.config.refresh_token,
                "scope": "Notes.Read.All offline_access",
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        self._delegated_token.access_token = data["access_token"]
        self._delegated_token.expires_at = time.time() + data.get("expires_in", 3600)

        # If a new refresh token is returned, update config
        if "refresh_token" in data:
            self.config.refresh_token = data["refresh_token"]
            log.info("Refresh token rotated by Azure AD")

        return self._delegated_token.access_token

    def _base_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    # ── Core Request ────────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        delegated: bool = False,
        ru_cost: int = 1,
        base_url: str = GRAPH_BASE,
    ) -> httpx.Response:
        """Make an authenticated Graph API request with retry and rate-limit handling."""
        # Pre-throttle if budget is low
        wait = self._budget.wait_seconds()
        if wait > 0:
            log.info("Pre-throttling %.1fs (RU budget %.0f%%)", wait, self._budget.units_used / self._budget.max_units_per_minute * 100)
            await asyncio.sleep(wait)

        token = (
            await self._acquire_delegated_token()
            if delegated
            else await self._acquire_app_token()
        )
        headers = self._base_headers(token)
        url = f"{base_url}{path}" if path.startswith("/") else path

        for attempt in range(MAX_RETRIES):
            try:
                resp = await self.http.request(
                    method, url, headers=headers, params=params, json=json_body,
                    timeout=60.0,
                )
            except httpx.TimeoutException:
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE ** attempt
                    log.warning("Timeout on %s %s, retrying in %.1fs", method, path, wait)
                    await asyncio.sleep(wait)
                    continue
                raise

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "30"))
                wait = min(retry_after, 120)
                log.warning("429 throttled, waiting %ds (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE ** attempt
                log.warning("Server error %d, retrying in %.1fs", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            self._budget.record(ru_cost)
            return resp

        # Return last response even if not successful — let caller handle
        return resp

    async def get(self, path: str, *, params: dict | None = None, **kwargs) -> dict:
        """GET request, returns parsed JSON."""
        resp = await self.request("GET", path, params=params, **kwargs)
        return resp.json()

    async def get_binary(self, path: str, **kwargs) -> bytes:
        """GET request, returns raw bytes (for file downloads)."""
        resp = await self.request("GET", path, **kwargs)
        return resp.content

    # ── Pagination ──────────────────────────────────────────────

    async def get_all_pages(
        self,
        path: str,
        *,
        params: dict | None = None,
        max_pages: int = 1000,
        **kwargs,
    ) -> list[dict]:
        """Follow @odata.nextLink pagination, collect all items."""
        items: list[dict] = []
        next_url = None

        for page in range(max_pages):
            if next_url:
                resp = await self.request("GET", next_url, base_url="", **kwargs)
                data = resp.json()
            else:
                data = await self.get(path, params=params, **kwargs)

            items.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
            if not next_url:
                break

        if next_url:
            log.warning("Pagination limit reached (%d pages) for %s", max_pages, path)

        return items

    # ── Delta Queries ───────────────────────────────────────────

    async def delta_query(
        self,
        path: str,
        *,
        delta_link: str | None = None,
        params: dict | None = None,
        max_pages: int = 5000,
        **kwargs,
    ) -> tuple[list[dict], str]:
        """Execute a delta query. Returns (items, new_delta_link).

        If delta_link is provided, resumes from that point.
        If not, performs initial full enumeration.
        """
        items: list[dict] = []
        new_delta_link = ""

        # Resume from delta link or start fresh
        if delta_link:
            url = delta_link
            use_base = ""
        else:
            url = path
            use_base = GRAPH_BASE

        for page in range(max_pages):
            if use_base:
                resp = await self.request(
                    "GET", url, params=params, ru_cost=RU_COSTS["delta"],
                    base_url=use_base, **kwargs,
                )
            else:
                resp = await self.request(
                    "GET", url, ru_cost=RU_COSTS["delta"], base_url="", **kwargs,
                )
            data = resp.json()

            items.extend(data.get("value", []))

            # Check for next page or delta link
            next_link = data.get("@odata.nextLink")
            delta = data.get("@odata.deltaLink")

            if delta:
                new_delta_link = delta
                break
            elif next_link:
                url = next_link
                use_base = ""
            else:
                break

        return items, new_delta_link

    # ── Batch Requests ──────────────────────────────────────────

    async def batch(
        self, requests: list[dict], **kwargs
    ) -> list[dict]:
        """Execute a $batch request (up to 20 individual requests).

        Each request dict: {"id": "1", "method": "GET", "url": "/path"}
        Returns list of response dicts: {"id": "1", "status": 200, "body": {...}}
        """
        if len(requests) > MAX_BATCH_SIZE:
            raise ValueError(f"Batch limit is {MAX_BATCH_SIZE}, got {len(requests)}")

        resp = await self.request(
            "POST",
            "/$batch",
            json_body={"requests": requests},
            ru_cost=len(requests),  # each request counted individually
            **kwargs,
        )
        data = resp.json()
        return data.get("responses", [])
