"""Tests for evaluation search with OPA filtering."""

from unittest.mock import AsyncMock, MagicMock
import pytest

import run_eval
from run_eval import check_opa_access


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear OPA access cache before each test."""
    run_eval._opa_access_cache.clear()
    yield
    run_eval._opa_access_cache.clear()


class TestCheckOpaAccess:
    async def test_allows_public(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        result = await check_opa_access(client, "public")
        assert result is True

    async def test_denies_restricted(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": False}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        result = await check_opa_access(client, "restricted")
        assert result is False

    async def test_caches_result(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        await check_opa_access(client, "public")
        await check_opa_access(client, "public")

        assert client.post.call_count == 1

    async def test_different_classifications_not_cached_together(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        await check_opa_access(client, "public")
        await check_opa_access(client, "internal")

        assert client.post.call_count == 2

    async def test_fail_closed_on_error(self):
        client = AsyncMock()
        client.post.side_effect = Exception("OPA down")

        result = await check_opa_access(client, "internal")
        assert result is False

    async def test_uses_eval_agent_role(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        await check_opa_access(client, "internal")

        call_args = client.post.call_args
        input_data = call_args[1]["json"]["input"]
        assert input_data["agent_role"] == "analyst"
        assert input_data["agent_id"] == "eval-bot"

    async def test_calls_correct_opa_endpoint(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        await check_opa_access(client, "public")

        call_args = client.post.call_args
        url = call_args[0][0]
        assert "/v1/data/kb/access/allow" in url

    async def test_input_includes_action_read(self):
        client = AsyncMock()
        response = MagicMock()
        response.json.return_value = {"result": True}
        response.raise_for_status = MagicMock()
        client.post.return_value = response

        await check_opa_access(client, "confidential")

        call_args = client.post.call_args
        input_data = call_args[1]["json"]["input"]
        assert input_data["action"] == "read"
        assert input_data["resource"] == "eval/search"
        assert input_data["classification"] == "confidential"
