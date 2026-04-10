"""Tests for the repo sync worker job."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.jobs.repo_sync import run


@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.http_client = AsyncMock()
    return ctx


async def test_sync_success(mock_ctx):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "repos": [
            {"status": "synced", "repo": "a"},
            {"status": "up_to_date", "repo": "b"},
            {"status": "error", "repo": "c"},
        ]
    }
    mock_ctx.http_client.post = AsyncMock(return_value=resp)

    result = await run(mock_ctx)

    assert result["repos"] == 3
    assert result["synced"] == 1
    assert result["errors"] == 1


async def test_sync_http_error(mock_ctx):
    mock_ctx.http_client.post = AsyncMock(side_effect=Exception("Connection refused"))

    result = await run(mock_ctx)

    assert "error" in result
    assert "Connection refused" in result["error"]


async def test_job_in_specs():
    """Verify repo_sync is registered in JOB_SPECS."""
    from worker.scheduler import JOB_SPECS
    ids = [s["id"] for s in JOB_SPECS]
    assert "repo_sync" in ids
