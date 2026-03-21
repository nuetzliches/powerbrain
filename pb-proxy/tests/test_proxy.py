"""Tests for the main proxy FastAPI application."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_deps():
    """Mock all external dependencies."""
    with patch("proxy.tool_injector") as mock_injector, \
         patch("proxy.check_opa_policy") as mock_opa, \
         patch("proxy.AgentLoop") as mock_loop_cls, \
         patch("proxy.llm_acompletion") as mock_acompletion:

        mock_injector.merge_tools = MagicMock(return_value=[
            {"type": "function", "function": {"name": "search_knowledge"}},
        ])
        mock_injector.tool_names = {"search_knowledge"}
        mock_opa.return_value = {"provider_allowed": True, "max_iterations": 10}

        mock_result = MagicMock()
        mock_result.response = MagicMock()
        mock_result.response.model_dump.return_value = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"content": "Hello!"}}],
        }
        mock_result.iterations = 1
        mock_result.tool_calls_executed = 0
        mock_result.max_iterations_reached = False
        mock_result.tools_used = []

        mock_loop = AsyncMock()
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_loop_cls.return_value = mock_loop

        yield {
            "injector": mock_injector,
            "opa": mock_opa,
            "loop_cls": mock_loop_cls,
            "loop": mock_loop,
            "result": mock_result,
            "acompletion": mock_acompletion,
        }


@pytest.fixture
def client(mock_deps):
    from proxy import app
    return TestClient(app)


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_chat_completions_requires_model(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )
    assert response.status_code == 422  # Validation error — model required


def test_chat_completions_success(client, mock_deps):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 200


def test_chat_completions_with_client_tools(client, mock_deps):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Use my tool"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "my_tool",
                        "description": "A custom tool",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        },
    )
    assert response.status_code == 200
    # Verify merge_tools was called with client tools
    mock_deps["injector"].merge_tools.assert_called_once()


def test_chat_completions_denied_by_opa(client, mock_deps):
    """OPA denial returns 403."""
    mock_deps["opa"].return_value = {"provider_allowed": False, "max_iterations": 0}
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )
    assert response.status_code == 403


def test_streaming_returns_501(client, mock_deps):
    """Streaming requests return 501 Not Implemented."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
    )
    assert response.status_code == 501


def test_agent_loop_receives_acompletion(client, mock_deps):
    """AgentLoop is created with the llm_acompletion callable."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 200
    mock_deps["loop_cls"].assert_called_once()
    call_kwargs = mock_deps["loop_cls"].call_args
    assert call_kwargs.kwargs.get("acompletion") is mock_deps["acompletion"]
