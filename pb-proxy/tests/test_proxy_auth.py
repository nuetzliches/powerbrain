"""Tests for proxy auth integration."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_verifier():
    """Mock ProxyKeyVerifier."""
    verifier = AsyncMock()
    verifier.verify = AsyncMock()
    return verifier


@pytest.fixture
def mock_base_deps():
    """Mock non-auth external dependencies for proxy."""
    with patch("proxy.tool_injector") as mock_injector, \
         patch("proxy.check_opa_policy") as mock_opa, \
         patch("proxy.AgentLoop") as mock_loop_cls, \
         patch("proxy.router_acompletion") as mock_router, \
         patch("proxy.direct_acompletion") as mock_direct, \
         patch("proxy.known_aliases", {"gpt-4o"}) as mock_aliases, \
         patch("proxy.pii_http_client") as mock_pii_http:

        mock_injector.merge_tools = MagicMock(return_value=[])
        mock_injector.tool_names = set()
        mock_opa.return_value = {"provider_allowed": True, "max_iterations": 10}

        # Default: PII scan returns no PII
        mock_pii_resp = MagicMock()
        mock_pii_resp.status_code = 200
        mock_pii_resp.raise_for_status = MagicMock()
        mock_pii_resp.json.return_value = {
            "text": "hi",
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
            "router": mock_router,
            "direct": mock_direct,
            "pii_http": mock_pii_http,
        }


def test_auth_required_no_header(mock_base_deps, mock_verifier):
    """Request without auth header returns 401 when AUTH_REQUIRED=true."""
    with patch("config.AUTH_REQUIRED", True), \
         patch("proxy.key_verifier", mock_verifier):
        from proxy import app
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 401


def test_auth_required_invalid_key(mock_base_deps, mock_verifier):
    """Request with invalid key returns 401."""
    mock_verifier.verify.return_value = None
    with patch("config.AUTH_REQUIRED", True), \
         patch("proxy.key_verifier", mock_verifier):
        from proxy import app
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer kb_invalid_key_12345678901234567890"},
        )
        assert response.status_code == 401


def test_auth_required_valid_key(mock_base_deps, mock_verifier):
    """Request with valid key returns 200 and sets agent identity."""
    mock_verifier.verify.return_value = {
        "agent_id": "test-agent",
        "agent_role": "analyst",
    }
    with patch("config.AUTH_REQUIRED", True), \
         patch("proxy.key_verifier", mock_verifier):
        from proxy import app
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer kb_valid_key_123456789012345678901"},
        )
        assert response.status_code == 200
        # OPA should have been called with verified agent_role
        mock_base_deps["opa"].assert_called_once()
        opa_call = mock_base_deps["opa"].call_args
        assert opa_call.args[0] == "analyst"


def test_auth_disabled_allows_anonymous(mock_base_deps, mock_verifier):
    """Request without auth succeeds when AUTH_REQUIRED=false."""
    with patch("config.AUTH_REQUIRED", False), \
         patch("proxy.key_verifier", mock_verifier):
        from proxy import app
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        # Verifier should not have been called
        mock_verifier.verify.assert_not_called()


def test_auth_disabled_provider_key_passthrough(mock_base_deps, mock_verifier):
    """In legacy mode, provider-style keys are still recognized for backward compat."""
    with patch("config.AUTH_REQUIRED", False), \
         patch("proxy.key_verifier", mock_verifier):
        from proxy import app
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-ant-some-provider-key-12345"},
        )
        assert response.status_code == 200
