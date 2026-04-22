"""Tests for shared.opa_client module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.opa_client import (
    OpaPolicyMissingError,
    opa_query,
    verify_required_policies,
)


def _response(body: dict | None, status: int = 200) -> MagicMock:
    """Build a mock httpx.Response with the given JSON body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status}", request=MagicMock(), response=resp,
        )
    return resp


def _client(response: MagicMock) -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = response
    return client


# ---------------------------------------------------------------------------
# opa_query
# ---------------------------------------------------------------------------

class TestOpaQueryReturnsResult:

    @pytest.mark.asyncio
    async def test_unwraps_result_field(self):
        client = _client(_response({"result": {"allowed": True, "min_score": 0.7}}))

        got = await opa_query(client, "http://opa:8181", "pb/ingestion/quality_gate",
                              input_data={"source_type": "code"})

        assert got == {"allowed": True, "min_score": 0.7}
        # URL is built via path form with slashes
        client.post.assert_called_once()
        assert client.post.call_args.args[0] == "http://opa:8181/v1/data/pb/ingestion/quality_gate"

    @pytest.mark.asyncio
    async def test_accepts_dot_notation(self):
        client = _client(_response({"result": True}))

        got = await opa_query(client, "http://opa:8181", "pb.access.allow",
                              input_data={"agent_role": "analyst"})

        assert got is True
        assert client.post.call_args.args[0] == "http://opa:8181/v1/data/pb/access/allow"

    @pytest.mark.asyncio
    async def test_accepts_leading_slash(self):
        client = _client(_response({"result": 1}))
        await opa_query(client, "http://opa:8181", "/pb/access/allow")
        assert client.post.call_args.args[0].endswith("/v1/data/pb/access/allow")

    @pytest.mark.asyncio
    async def test_empty_input_defaults_to_empty_dict(self):
        client = _client(_response({"result": 1}))
        await opa_query(client, "http://opa:8181", "pb/access/allow")
        assert client.post.call_args.kwargs["json"] == {"input": {}}

    @pytest.mark.asyncio
    async def test_passes_through_false_result(self):
        """A `result: false` is a valid deny decision — must NOT raise."""
        client = _client(_response({"result": False}))
        got = await opa_query(client, "http://opa:8181", "pb/access/allow")
        assert got is False

    @pytest.mark.asyncio
    async def test_passes_through_empty_dict_result(self):
        """`result: {}` is valid (rule returned empty dict) — must NOT raise."""
        client = _client(_response({"result": {}}))
        got = await opa_query(client, "http://opa:8181", "pb/ingestion/quality_gate")
        assert got == {}


class TestOpaQueryRaisesOnMissingResult:

    @pytest.mark.asyncio
    async def test_missing_result_field_raises(self):
        """OPA returns only decision_id when no rule matches — must raise."""
        client = _client(_response({"decision_id": "abc-123"}))

        with pytest.raises(OpaPolicyMissingError) as exc_info:
            await opa_query(client, "http://opa:8181", "pb/ingestion/quality_gate")

        assert exc_info.value.package_path == "pb/ingestion/quality_gate"
        assert "not loaded" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_body_raises(self):
        client = _client(_response({}))
        with pytest.raises(OpaPolicyMissingError):
            await opa_query(client, "http://opa:8181", "pb/access/allow")

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        client = _client(_response({"error": "bad"}, status=500))
        with pytest.raises(httpx.HTTPStatusError):
            await opa_query(client, "http://opa:8181", "pb/access/allow")


# ---------------------------------------------------------------------------
# verify_required_policies
# ---------------------------------------------------------------------------

class TestVerifyRequiredPolicies:

    @pytest.mark.asyncio
    async def test_all_loaded_returns_normally(self):
        client = _client(_response({"result": {}}))
        # Should not raise
        await verify_required_policies(
            client, "http://opa:8181",
            ["pb/access/allow", "pb/ingestion/quality_gate"],
        )
        assert client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_one_missing_raises_runtime_error(self):
        responses = iter([
            _response({"result": True}),          # pb/access/allow → loaded
            _response({"decision_id": "x"}),      # pb/ingestion/quality_gate → missing
        ])
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.side_effect = lambda *a, **k: next(responses)

        with pytest.raises(RuntimeError) as exc_info:
            await verify_required_policies(
                client, "http://opa:8181",
                ["pb/access/allow", "pb/ingestion/quality_gate"],
            )

        assert "pb/ingestion/quality_gate" in str(exc_info.value)
        assert "pb/access/allow" not in str(exc_info.value) or "missing" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_network_error_surfaces_as_missing(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.side_effect = httpx.ConnectError("refused")

        with pytest.raises(RuntimeError) as exc_info:
            await verify_required_policies(
                client, "http://opa:8181", ["pb/access/allow"],
            )

        msg = str(exc_info.value)
        assert "pb/access/allow" in msg
        assert "probe error" in msg

    @pytest.mark.asyncio
    async def test_false_result_is_not_missing(self):
        """A rule returning `false` for empty input means 'loaded, denies default'.

        The startup check must NOT treat that as missing — otherwise legitimate
        deny-by-default rules would prevent boot.
        """
        client = _client(_response({"result": False}))
        await verify_required_policies(
            client, "http://opa:8181", ["pb/access/allow"],
        )
