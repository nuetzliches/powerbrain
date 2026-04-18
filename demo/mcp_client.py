"""Thin MCP HTTP client for the Powerbrain sales-demo UI.

Owns all request/response parsing. If the upstream schema changes, the
Pydantic validators here will raise a clear "Demo out of date" message
instead of crashing mid-presentation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel, Field, ValidationError


class DemoOutOfDateError(RuntimeError):
    """MCP returned an unexpected shape — demo was built against an older API."""


class SearchResultItem(BaseModel):
    id: str
    score: float = 0.0
    rerank_score: float = 0.0
    rank: int = 0
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Populated when the caller supplies a valid pii_access_token + purpose
    # and OPA authorises vault access.
    original_content: str | None = None
    vault_access: bool = False


class SearchResponse(BaseModel):
    results: list[SearchResultItem] = Field(default_factory=list)
    total: int = 0
    summary: str | None = None
    summary_policy: str | None = None


class GraphNode(BaseModel):
    # AGE returns nodes as {"id": <int>, "label": str, "properties": {...}}
    # but find_node/get_neighbors wrap them — keep the shape loose.
    id: int | str | None = None
    label: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class _MCPClient:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.url = (url or os.environ.get("MCP_URL", "http://localhost:8080")).rstrip("/")
        self.api_key = api_key or os.environ.get("MCP_API_KEY", "")
        self.timeout = timeout
        self._request_id = 0

    def _headers(self, override_key: str | None = None) -> dict[str, str]:
        key = override_key or self.api_key
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _rpc(self, method: str, params: dict, api_key: str | None = None) -> dict:
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        resp = requests.post(
            f"{self.url}/mcp",
            headers=self._headers(api_key),
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json() if resp.text else {}
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data

    def initialize(self, api_key: str | None = None) -> None:
        self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pb-demo-ui", "version": "1.0"},
        }, api_key=api_key)

    def call_tool(
        self,
        name: str,
        arguments: dict,
        api_key: str | None = None,
    ) -> dict:
        """Call an MCP tool and return the parsed JSON inside content[0].text."""
        resp = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            api_key=api_key,
        )
        try:
            text = resp["result"]["content"][0]["text"]
            return json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise DemoOutOfDateError(
                f"Unexpected MCP response for {name}: {exc}\n"
                f"Full payload: {resp}"
            )

    # ── Convenience wrappers ────────────────────────────────────────

    def search_knowledge(
        self,
        query: str,
        api_key: str,
        collection: str = "pb_general",
        top_k: int = 5,
        pii_access_token: dict | None = None,
        purpose: str | None = None,
    ) -> SearchResponse:
        args: dict[str, Any] = {
            "query": query,
            "collection": collection,
            "top_k": top_k,
        }
        if pii_access_token and purpose:
            args["pii_access_token"] = pii_access_token
            args["purpose"] = purpose
        raw = self.call_tool("search_knowledge", args, api_key=api_key)
        try:
            return SearchResponse(**raw)
        except ValidationError as exc:
            raise DemoOutOfDateError(
                f"search_knowledge response shape changed: {exc}"
            )

    def graph_query(self, action: str, api_key: str, **kwargs) -> dict:
        return self.call_tool(
            "graph_query",
            {"action": action, **kwargs},
            api_key=api_key,
        )

    def query_data(
        self,
        dataset: str,
        api_key: str,
        conditions: dict | None = None,
        limit: int = 50,
    ) -> dict:
        args: dict[str, Any] = {"dataset": dataset, "limit": limit}
        if conditions:
            args["conditions"] = conditions
        return self.call_tool("query_data", args, api_key=api_key)


class _IngestionClient:
    """Light wrapper for the ingestion service (bypasses MCP for PII ingest demo)."""

    def __init__(self, url: str | None = None, timeout: int = 60) -> None:
        self.url = (
            url or os.environ.get("INGESTION_URL", "http://localhost:8081")
        ).rstrip("/")
        self.timeout = timeout

    def ingest(
        self,
        content: str,
        *,
        collection: str = "pb_general",
        classification: str = "confidential",
        project: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "source": content,
            "source_type": "text",
            "collection": collection,
            "classification": classification,
            "metadata": metadata or {},
        }
        if project:
            payload["project"] = project
        resp = requests.post(f"{self.url}/ingest", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def scan(self, text: str) -> dict:
        """Run PII scan on a text snippet (used to show which entities would be redacted)."""
        resp = requests.post(
            f"{self.url}/scan",
            json={"text": text},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def preview(
        self,
        *,
        text: str | None = None,
        data_b64: str | None = None,
        filename: str | None = None,
        classification: str = "internal",
        source_type: str = "default",
        language: str = "de",
        legal_basis: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Dry-run the ingestion pipeline via POST /preview (no persistence).

        Powers the sales-demo Pipeline Inspector. Caller passes either
        ``text`` (already extracted) or ``data_b64`` + ``filename`` for
        the extractor to run end-to-end.
        """
        payload: dict[str, Any] = {
            "classification": classification,
            "source_type":    source_type,
            "language":       language,
            "metadata":       metadata or {},
        }
        if text is not None:
            payload["text"] = text
        if data_b64 is not None:
            payload["data"] = data_b64
        if filename is not None:
            payload["filename"] = filename
        if legal_basis:
            payload["legal_basis"] = legal_basis

        resp = requests.post(
            f"{self.url}/preview",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def _load_vault_secret() -> str | None:
    """Read the vault HMAC secret from a Docker secret file or env var.

    Demo-only: in production, only trusted services hold this secret.
    """
    file_path = os.environ.get("VAULT_HMAC_SECRET_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except Exception:
            pass
    env_val = os.environ.get("VAULT_HMAC_SECRET")
    if env_val:
        return env_val.strip()
    return None


def build_vault_token(
    purpose: str,
    data_category: str,
    ttl_minutes: int = 10,
    secret: str | None = None,
) -> dict:
    """Build an HMAC-signed PII access token (server validates it)."""
    secret = secret or _load_vault_secret()
    if not secret:
        raise RuntimeError(
            "VAULT_HMAC_SECRET not available — cannot issue vault token. "
            "Mount secrets/vault_hmac_secret.txt into the demo container."
        )
    payload = {
        "purpose": purpose,
        "data_category": data_category,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat(),
    }
    signature = hmac.new(
        secret.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {**payload, "signature": signature}


class _ProxyClient:
    """Thin client for pb-proxy's OpenAI-compatible ``/v1/chat/completions``.

    Used by the sales-demo "MCP vs Proxy" tab to demonstrate the
    enterprise-edition path (vault resolution + tool injection + chat)
    next to the raw MCP path.
    """

    def __init__(self, url: str | None = None, timeout: int = 60) -> None:
        self.url = (
            url or os.environ.get("PROXY_URL", "")
        ).rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        """``True`` if a pb-proxy URL is configured and /health responds."""
        if not self.url:
            return False
        try:
            resp = requests.get(f"{self.url}/health", timeout=3)
        except Exception:
            return False
        return resp.status_code == 200

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        api_key: str,
        purpose: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 400,
    ) -> dict:
        """Call pb-proxy /v1/chat/completions. Returns the raw JSON body.

        ``purpose`` is forwarded as the ``X-Purpose`` header so the
        proxy's ``pb.proxy.pii_resolve_tool_results`` policy can decide
        whether to de-pseudonymise tool results via ``/vault/resolve``.
        """
        if not self.url:
            raise RuntimeError("PROXY_URL not configured")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if purpose:
            headers["X-Purpose"] = purpose

        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = requests.post(
            f"{self.url}/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=self.timeout,
        )
        # Don't raise_for_status — the UI needs to show error bodies too.
        try:
            return resp.json()
        except Exception:
            return {"_error": resp.text[:2000]}


def get_clients() -> tuple[_MCPClient, _IngestionClient]:
    return _MCPClient(), _IngestionClient()


def get_proxy_client() -> _ProxyClient:
    return _ProxyClient()
