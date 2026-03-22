"""Tests for the main proxy FastAPI application."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

import httpx


@pytest.fixture
def mock_deps():
    """Mock all external dependencies."""
    with patch("proxy.tool_injector") as mock_injector, \
         patch("proxy.check_opa_policy") as mock_opa, \
         patch("proxy.AgentLoop") as mock_loop_cls, \
         patch("proxy.llm_acompletion") as mock_acompletion, \
         patch("proxy.pii_http_client") as mock_pii_http:

        mock_injector.merge_tools = MagicMock(return_value=[
            {"type": "function", "function": {"name": "search_knowledge"}},
        ])
        mock_injector.tool_names = {"search_knowledge"}
        mock_opa.return_value = {"provider_allowed": True, "max_iterations": 10}

        # Default: PII scan returns no PII (pass-through)
        mock_pii_resp = MagicMock()
        mock_pii_resp.status_code = 200
        mock_pii_resp.raise_for_status = MagicMock()
        mock_pii_resp.json.return_value = {
            "text": "Hello!",
            "mapping": {},
            "contains_pii": False,
            "entity_types": [],
        }
        mock_pii_http.post = AsyncMock(return_value=mock_pii_resp)

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
            "pii_http": mock_pii_http,
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


def test_list_models_returns_configured_models(mock_deps):
    """GET /v1/models returns models from litellm_config.yaml."""
    import proxy
    original = proxy.model_list
    proxy.model_list = [
        {
            "model_name": "gpt-4o",
            "litellm_params": {"model": "github/gpt-4o", "api_key": "test"},
        },
        {
            "model_name": "claude-sonnet",
            "litellm_params": {"model": "anthropic/claude-sonnet-4-20250514"},
        },
    ]
    try:
        from proxy import app
        client = TestClient(app)
        response = client.get("/v1/models")
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 2
        ids = [m["id"] for m in data["data"]]
        assert "gpt-4o" in ids
        assert "claude-sonnet" in ids
        # Check owned_by is extracted from provider prefix
        owners = {m["id"]: m["owned_by"] for m in data["data"]}
        assert owners["gpt-4o"] == "github"
        assert owners["claude-sonnet"] == "anthropic"
        # All entries have correct object type
        assert all(m["object"] == "model" for m in data["data"])
    finally:
        proxy.model_list = original


def test_list_models_empty_when_no_config(mock_deps):
    """GET /v1/models returns empty list when no models configured."""
    import proxy
    original = proxy.model_list
    proxy.model_list = []
    try:
        from proxy import app
        client = TestClient(app)
        response = client.get("/v1/models")
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        assert data["data"] == []
    finally:
        proxy.model_list = original


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


def test_streaming_returns_sse(client, mock_deps):
    """Streaming requests return SSE event stream."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "data: " in body
    assert "data: [DONE]" in body


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


# ── User API Key Passthrough Tests ───────────────────────────


def test_user_api_key_passed_to_agent_loop(client, mock_deps):
    """Bearer token from Authorization header is forwarded as api_key."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        headers={"Authorization": "Bearer sk-ant-user-key-123"},
    )
    assert response.status_code == 200
    # Verify api_key was passed through litellm_kwargs to loop.run
    run_call = mock_deps["loop"].run.call_args
    assert run_call.kwargs.get("api_key") == "sk-ant-user-key-123"


def test_no_auth_header_means_no_api_key_override(client, mock_deps):
    """Without Authorization header, no api_key is passed (central key used)."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 200
    run_call = mock_deps["loop"].run.call_args
    assert "api_key" not in run_call.kwargs


def test_empty_bearer_token_ignored(client, mock_deps):
    """Empty Bearer token is treated as no key (fallback to central)."""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        headers={"Authorization": "Bearer "},
    )
    assert response.status_code == 200
    run_call = mock_deps["loop"].run.call_args
    assert "api_key" not in run_call.kwargs


# ── PII Protection Tests ─────────────────────────────────────


class TestPIIProtection:
    def test_messages_pseudonymized_before_llm(self, client, mock_deps):
        """User messages are pseudonymized before they reach the LLM."""
        # Configure PII mock to return pseudonymized text
        pii_resp = MagicMock()
        pii_resp.status_code = 200
        pii_resp.raise_for_status = MagicMock()
        pii_resp.json.return_value = {
            "text": "[PERSON:a1b2c3d4] braucht Hilfe",
            "mapping": {"Sebastian": "[PERSON:a1b2c3d4]"},
            "contains_pii": True,
            "entity_types": ["PERSON"],
        }
        mock_deps["pii_http"].post = AsyncMock(return_value=pii_resp)

        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Sebastian braucht Hilfe"}],
        })
        assert response.status_code == 200

        # Verify AgentLoop received pii_reverse_map
        call_kwargs = mock_deps["loop_cls"].call_args
        assert call_kwargs.kwargs.get("pii_reverse_map") == {
            "[PERSON:a1b2c3d4]": "Sebastian"
        }

        # Verify loop.run received pseudonymized messages
        run_call = mock_deps["loop"].run.call_args
        messages_sent = run_call.kwargs.get("messages") or run_call.args[1]
        # System hint should be injected as first message
        assert any(
            "Pseudonyme" in m.get("content", "")
            for m in messages_sent if m["role"] == "system"
        )

    def test_response_depseudonymized(self, client, mock_deps):
        """LLM response is de-pseudonymized before returning to user."""
        # Configure PII mock
        pii_resp = MagicMock()
        pii_resp.status_code = 200
        pii_resp.raise_for_status = MagicMock()
        pii_resp.json.return_value = {
            "text": "[PERSON:a1b2c3d4] braucht Hilfe",
            "mapping": {"Sebastian": "[PERSON:a1b2c3d4]"},
            "contains_pii": True,
            "entity_types": ["PERSON"],
        }
        mock_deps["pii_http"].post = AsyncMock(return_value=pii_resp)

        # Configure LLM response to contain pseudonyms
        mock_deps["result"].response.model_dump.return_value = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"content": "[PERSON:a1b2c3d4] sollte den Termin bestätigen."}}],
        }

        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Sebastian braucht Hilfe"}],
        })
        assert response.status_code == 200
        data = response.json()
        # Response should contain original name, not pseudonym
        assert data["choices"][0]["message"]["content"] == "Sebastian sollte den Termin bestätigen."

    def test_pii_scan_fail_open_when_not_forced(self, client, mock_deps):
        """When PII scan fails and not forced, request continues (fail-open)."""
        mock_deps["pii_http"].post = AsyncMock(
            side_effect=httpx.ConnectError("ingestion down")
        )

        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Sebastian Test"}],
        })
        # Should succeed (fail-open) since pii_scan_forced defaults to False
        assert response.status_code == 200

    def test_pii_scan_fail_closed_when_forced(self, mock_deps):
        """When PII scan fails and forced, returns 503."""
        mock_deps["opa"].return_value = {
            "provider_allowed": True,
            "max_iterations": 10,
            "pii_scan_enabled": True,
            "pii_scan_forced": True,
        }
        mock_deps["pii_http"].post = AsyncMock(
            side_effect=httpx.ConnectError("ingestion down")
        )

        from proxy import app
        test_client = TestClient(app)
        response = test_client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Sebastian Test"}],
        })
        assert response.status_code == 503

    def test_non_text_content_filtered(self, client, mock_deps):
        """Non-text content (images) is filtered before PII scan."""
        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Was zeigt das Bild?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ]}],
        })
        assert response.status_code == 200

    def test_no_pii_no_reverse_map(self, client, mock_deps):
        """When no PII found, reverse map is empty."""
        response = client.post("/v1/chat/completions", json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hallo Welt"}],
        })
        assert response.status_code == 200

        # AgentLoop should have been created with empty pii_reverse_map
        call_kwargs = mock_deps["loop_cls"].call_args
        assert call_kwargs.kwargs.get("pii_reverse_map") == {}
