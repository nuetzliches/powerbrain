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
         patch("proxy.router_acompletion") as mock_router_acompletion, \
         patch("proxy.direct_acompletion") as mock_direct_acompletion, \
         patch("proxy.known_aliases", {"gpt-4o", "claude-opus", "gpt-4", "claude-sonnet"}) as mock_aliases, \
         patch("proxy.pii_http_client") as mock_pii_http, \
         patch("proxy.key_verifier") as mock_verifier, \
         patch("config.AUTH_REQUIRED", False):

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
            "router_acompletion": mock_router_acompletion,
            "direct_acompletion": mock_direct_acompletion,
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
    # Edition label exposes the pb-proxy tier explicitly so dashboards
    # and the sales demo can detect which layer is in front of MCP.
    assert data["service"] == "pb-proxy"
    assert data["edition"] == "enterprise"


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


def test_metrics_json_endpoint(client):
    """GET /metrics/json returns structured metrics."""
    response = client.get("/metrics/json")
    assert response.status_code == 200
    data = response.json()
    
    # Check basic structure
    assert "service" in data
    assert "uptime_seconds" in data
    assert "requests" in data
    assert "latency" in data
    assert "agent_loop" in data
    assert "tool_calls" in data
    assert "pii" in data
    
    # Check nested structure
    assert "total" in data["requests"]
    assert "by_model" in data["requests"]
    assert "by_status" in data["requests"]
    assert "by_model" in data["latency"]
    assert "total" in data["tool_calls"]
    assert "by_tool" in data["tool_calls"]
    assert "entities_pseudonymized" in data["pii"]
    assert "scan_failures" in data["pii"]
    
    # Basic type checks
    assert isinstance(data["uptime_seconds"], (int, float))
    assert isinstance(data["requests"]["total"], (int, float))
    assert isinstance(data["requests"]["by_model"], dict)
    assert isinstance(data["requests"]["by_status"], dict)
    assert isinstance(data["tool_calls"]["total"], (int, float))
    assert data["service"] == "pb-proxy"


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


def test_streaming_includes_telemetry_chunk(mock_deps):
    """SSE stream includes _telemetry chunk before [DONE] when enabled."""
    import proxy

    # Inject _telemetry into the mock response data
    mock_deps["result"].response.model_dump.return_value = {
        "id": "chatcmpl-123",
        "model": "gpt-4o",
        "choices": [{"message": {"content": "Hi!"}}],
        "_telemetry": {
            "trace_id": "abc123",
            "steps": [{"name": "pii_pseudonymize", "duration_ms": 5.0}],
        },
    }

    original = proxy.TELEMETRY_IN_RESPONSE
    proxy.TELEMETRY_IN_RESPONSE = True
    try:
        from proxy import app
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        body = response.text
        assert "_telemetry" in body
        # Telemetry chunk must appear before [DONE]
        telemetry_pos = body.index("_telemetry")
        done_pos = body.index("[DONE]")
        assert telemetry_pos < done_pos
    finally:
        proxy.TELEMETRY_IN_RESPONSE = original


def test_agent_loop_receives_acompletion(client, mock_deps):
    """AgentLoop is created with the correct acompletion callable."""
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
    # gpt-4o is a known alias, so Router acompletion is used
    assert call_kwargs.kwargs.get("acompletion") is mock_deps["router_acompletion"]


# ── User API Key Tests ───────────────────────────────────────


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
            "pseudonyms" in m.get("content", "").lower()
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

    def test_pii_scan_fail_open_when_not_forced(self, mock_deps):
        """When PII scan fails and not forced, request continues (fail-open)."""
        mock_deps["opa"].return_value = {
            "provider_allowed": True,
            "max_iterations": 10,
            "pii_scan_enabled": True,
            "pii_scan_forced": False,
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
        # Should succeed (fail-open) since pii_scan_forced explicitly False
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

    def test_pii_disabled_records_skipped_telemetry(self, mock_deps):
        """When PII scan disabled, response includes skipped telemetry step."""
        import proxy

        mock_deps["opa"].return_value = {
            "provider_allowed": True,
            "max_iterations": 10,
            "pii_scan_enabled": False,
        }
        mock_deps["result"].response.model_dump.return_value = {
            "id": "chatcmpl-123",
            "choices": [{"message": {"content": "Hello!"}}],
        }

        original = proxy.TELEMETRY_IN_RESPONSE
        proxy.TELEMETRY_IN_RESPONSE = True
        try:
            from proxy import app
            test_client = TestClient(app)
            response = test_client.post("/v1/chat/completions", json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hallo"}],
            })
            assert response.status_code == 200
            data = response.json()
            telemetry = data.get("_telemetry", {})
            steps = telemetry.get("steps", [])
            pii_steps = [s for s in steps if s["name"] == "pii_pseudonymize"]
            assert len(pii_steps) == 1
            assert pii_steps[0]["status"] == "skipped"
            # metadata is flattened into the step dict by PipelineStep.to_dict()
            assert pii_steps[0]["mode"] == "disabled"
        finally:
            proxy.TELEMETRY_IN_RESPONSE = original

    def test_pii_fail_closed_records_telemetry(self, mock_deps):
        """When PII scan fails in forced mode, 503 response is returned."""
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
        assert "PII protection required" in response.json()["detail"]


# ── Passthrough Routing Tests ──────────────────────────────────


def test_passthrough_model_uses_direct_completion(mock_deps):
    """Models with provider/ prefix bypass Router and use litellm.acompletion."""
    with patch.dict("config.PROVIDER_KEY_MAP", {"anthropic": "sk-ant-test-key-123456"}):
        from proxy import app
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "anthropic/claude-3-5-haiku-20241022",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200
        # Verify the model name was passed through to the loop
        run_call = mock_deps["loop"].run.call_args
        assert run_call.kwargs["model"] == "anthropic/claude-3-5-haiku-20241022"
        # Verify direct_acompletion was used (not router)
        call_kwargs = mock_deps["loop_cls"].call_args
        assert call_kwargs.kwargs.get("acompletion") is mock_deps["direct_acompletion"]


def test_passthrough_no_key_returns_401(mock_deps):
    """Passthrough model with no API key configured returns 401."""
    with patch.dict("config.PROVIDER_KEY_MAP", {}, clear=True):
        from proxy import app
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "cohere/command-r-plus",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 401
        assert "cohere" in response.json()["detail"].lower()


def test_unknown_model_no_prefix_returns_400(mock_deps):
    """Model without provider prefix and not an alias returns 400."""
    from proxy import app
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "some-random-model",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )
    assert response.status_code == 400
    assert "provider/model-name" in response.json()["detail"]


def test_passthrough_with_env_key(mock_deps):
    """Passthrough uses PROVIDER_KEY_MAP when no user key provided."""
    with patch.dict("config.PROVIDER_KEY_MAP", {"anthropic": "sk-ant-central-key-123"}):
        from proxy import app
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "anthropic/claude-3-5-haiku-20241022",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200
        run_call = mock_deps["loop"].run.call_args
        assert run_call.kwargs.get("api_key") == "sk-ant-central-key-123"
