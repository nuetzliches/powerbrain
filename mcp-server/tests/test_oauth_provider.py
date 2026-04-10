"""Tests for mcp-server/oauth_provider.py — OAuth authorization server."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp.server.auth.provider import AccessToken, AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from oauth_provider import (
    PowerbrainOAuthProvider,
    CombinedTokenVerifier,
    PendingLogin,
    PbAccessToken,
    PbRefreshToken,
    PbAuthorizationCode,
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
    CODE_TTL,
    PENDING_LOGIN_TTL,
)


# ── Fixtures ──────────────────────────────────────────────


def _mock_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    pool.fetchval.return_value = None
    pool.execute.return_value = "INSERT 0 1"
    return pool


def _mock_verifier(*, returns=None):
    v = AsyncMock()
    v.verify_token.return_value = returns
    return v


def _make_client(client_id="client-1"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="secret",
        redirect_uris=["https://example.com/callback"],
        grant_types=["authorization_code"],
        response_types=["code"],
    )


def _make_params():
    return AuthorizationParams(
        state="state123",
        scopes=["read"],
        code_challenge="challenge",
        redirect_uri="https://example.com/callback",
        redirect_uri_provided_explicitly=True,
    )


def _provider(verifier=None, pool=None):
    v = verifier or _mock_verifier()
    p = pool or _mock_pool()

    async def get_pool():
        return p

    provider = PowerbrainOAuthProvider(
        api_key_verifier=v,
        login_url="https://mcp.local/login",
        callback_url="https://mcp.local/callback",
        get_pool=get_pool,
    )
    return provider, p, v


# ── Authorize ─────────────────────────────────────────────


class TestAuthorize:
    async def test_stores_pending_and_redirects(self):
        prov, _, _ = _provider()
        url = await prov.authorize(_make_client(), _make_params())
        assert "https://mcp.local/login?" in url
        assert "session=" in url
        assert len(prov._pending_logins) == 1

    async def test_multiple_sessions_unique(self):
        prov, _, _ = _provider()
        url1 = await prov.authorize(_make_client(), _make_params())
        url2 = await prov.authorize(_make_client(), _make_params())
        assert url1 != url2
        assert len(prov._pending_logins) == 2


# ── Handle Login Callback ────────────────────────────────


class TestHandleLoginCallback:
    async def test_valid_key_returns_redirect(self):
        access = AccessToken(token="t", client_id="agent-1", scopes=["read"])
        prov, _, _ = _provider(verifier=_mock_verifier(returns=access))
        url = await prov.authorize(_make_client(), _make_params())
        session_id = url.split("session=")[1]

        redirect, error = await prov.handle_login_callback(session_id, "pb_valid_key")
        assert error is None
        assert redirect is not None
        assert "code=" in redirect

    async def test_expired_session_returns_error(self):
        prov, _, _ = _provider()
        _, error = await prov.handle_login_callback("nonexistent", "pb_key")
        assert error is not None
        assert "expired" in error.lower()

    async def test_empty_key_returns_error(self):
        prov, _, _ = _provider()
        await prov.authorize(_make_client(), _make_params())
        session_id = list(prov._pending_logins.keys())[0]

        _, error = await prov.handle_login_callback(session_id, "")
        assert error is not None
        assert "enter" in error.lower()

    async def test_blank_key_returns_error(self):
        prov, _, _ = _provider()
        await prov.authorize(_make_client(), _make_params())
        session_id = list(prov._pending_logins.keys())[0]

        _, error = await prov.handle_login_callback(session_id, "   ")
        assert error is not None

    async def test_invalid_key_returns_error(self):
        prov, _, _ = _provider(verifier=_mock_verifier(returns=None))
        await prov.authorize(_make_client(), _make_params())
        session_id = list(prov._pending_logins.keys())[0]

        _, error = await prov.handle_login_callback(session_id, "pb_bad")
        assert "invalid" in error.lower()

    async def test_consumes_pending_login(self):
        access = AccessToken(token="t", client_id="a", scopes=[])
        prov, _, _ = _provider(verifier=_mock_verifier(returns=access))
        await prov.authorize(_make_client(), _make_params())
        session_id = list(prov._pending_logins.keys())[0]

        await prov.handle_login_callback(session_id, "pb_key")
        assert session_id not in prov._pending_logins


# ── Authorization Code ────────────────────────────────────


class TestAuthorizationCode:
    def _seed_code(self, prov, client_id="client-1", expired=False):
        now = time.time()
        code = PbAuthorizationCode(
            code="code123",
            client_id=client_id,
            redirect_uri="https://example.com/callback",
            redirect_uri_provided_explicitly=True,
            code_challenge="ch",
            scopes=["read"],
            expires_at=now - 10 if expired else now + CODE_TTL,
            api_key="pb_key",
        )
        prov._auth_codes["code123"] = code
        return code

    async def test_load_code_valid(self):
        prov, _, _ = _provider()
        self._seed_code(prov)
        client = _make_client()
        result = await prov.load_authorization_code(client, "code123")
        assert result is not None
        assert result.code == "code123"

    async def test_load_code_wrong_client(self):
        prov, _, _ = _provider()
        self._seed_code(prov, client_id="other-client")
        client = _make_client("client-1")
        result = await prov.load_authorization_code(client, "code123")
        assert result is None

    async def test_load_code_expired(self):
        prov, _, _ = _provider()
        self._seed_code(prov, expired=True)
        client = _make_client()
        result = await prov.load_authorization_code(client, "code123")
        assert result is None
        assert "code123" not in prov._auth_codes


# ── Exchange Authorization Code ───────────────────────────


class TestExchangeAuthorizationCode:
    async def test_creates_tokens(self):
        prov, pool, _ = _provider()
        code = PbAuthorizationCode(
            code="c1", client_id="client-1",
            redirect_uri="https://x.com/cb",
            redirect_uri_provided_explicitly=True,
            code_challenge="ch", scopes=["read"],
            expires_at=time.time() + 300, api_key="pb_k",
        )
        prov._auth_codes["c1"] = code
        client = _make_client()

        token = await prov.exchange_authorization_code(client, code)
        assert token.access_token is not None
        assert token.refresh_token is not None
        assert token.token_type.lower() == "bearer"
        assert token.expires_in == ACCESS_TOKEN_TTL

    async def test_consumes_code(self):
        prov, _, _ = _provider()
        code = PbAuthorizationCode(
            code="c2", client_id="client-1",
            redirect_uri="https://x.com/cb",
            redirect_uri_provided_explicitly=True,
            code_challenge="ch", scopes=[],
            expires_at=time.time() + 300, api_key="pb_k",
        )
        prov._auth_codes["c2"] = code
        await prov.exchange_authorization_code(_make_client(), code)
        assert "c2" not in prov._auth_codes

    async def test_stores_refresh_in_db(self):
        prov, pool, _ = _provider()
        code = PbAuthorizationCode(
            code="c3", client_id="client-1",
            redirect_uri="https://x.com/cb",
            redirect_uri_provided_explicitly=True,
            code_challenge="ch", scopes=["read"],
            expires_at=time.time() + 300, api_key="pb_k",
        )
        prov._auth_codes["c3"] = code
        await prov.exchange_authorization_code(_make_client(), code)
        # DB INSERT for refresh token
        pool.execute.assert_called_once()
        call_sql = pool.execute.call_args[0][0]
        assert "oauth_refresh_tokens" in call_sql


# ── Refresh Token ─────────────────────────────────────────


class TestRefreshToken:
    async def test_load_valid(self):
        prov, pool, _ = _provider()
        now = time.time()
        row = {
            "token": "rt_1", "client_id": "client-1",
            "api_key": "pb_k", "scopes": '["read"]',
            "expires_at": int(now + REFRESH_TOKEN_TTL),
        }
        pool.fetchrow.return_value = row
        client = _make_client()

        result = await prov.load_refresh_token(client, "rt_1")
        assert result is not None
        assert result.token == "rt_1"
        assert result.api_key == "pb_k"

    async def test_load_expired_deletes(self):
        prov, pool, _ = _provider()
        row = {
            "token": "rt_2", "client_id": "client-1",
            "api_key": "pb_k", "scopes": "[]",
            "expires_at": int(time.time() - 100),
        }
        pool.fetchrow.return_value = row
        client = _make_client()

        result = await prov.load_refresh_token(client, "rt_2")
        assert result is None
        pool.execute.assert_called_once()  # DELETE

    async def test_load_wrong_client(self):
        prov, pool, _ = _provider()
        row = {
            "token": "rt_3", "client_id": "other-client",
            "api_key": "pb_k", "scopes": "[]",
            "expires_at": int(time.time() + 3600),
        }
        pool.fetchrow.return_value = row
        client = _make_client("client-1")

        result = await prov.load_refresh_token(client, "rt_3")
        assert result is None


# ── Access Token ──────────────────────────────────────────


class TestAccessToken:
    async def test_load_valid(self):
        prov, _, _ = _provider()
        prov._access_tokens["at_1"] = PbAccessToken(
            token="at_1", client_id="c1", scopes=["read"],
            expires_at=int(time.time() + 3600), api_key="pb_k",
        )
        result = await prov.load_access_token("at_1")
        assert result is not None
        assert result.token == "at_1"

    async def test_load_expired(self):
        prov, _, _ = _provider()
        prov._access_tokens["at_2"] = PbAccessToken(
            token="at_2", client_id="c1", scopes=[],
            expires_at=int(time.time() - 10), api_key="pb_k",
        )
        result = await prov.load_access_token("at_2")
        assert result is None
        assert "at_2" not in prov._access_tokens

    async def test_load_missing(self):
        prov, _, _ = _provider()
        result = await prov.load_access_token("nonexistent")
        assert result is None


# ── Revocation ────────────────────────────────────────────


class TestRevoke:
    async def test_revoke_access_token(self):
        prov, _, _ = _provider()
        at = PbAccessToken(
            token="at_r", client_id="c", scopes=[],
            expires_at=int(time.time() + 3600), api_key="pb_k",
        )
        prov._access_tokens["at_r"] = at
        await prov.revoke_token(at)
        assert "at_r" not in prov._access_tokens

    async def test_revoke_refresh_token(self):
        prov, pool, _ = _provider()
        rt = PbRefreshToken(
            token="rt_r", client_id="c", scopes=[],
            expires_at=int(time.time() + 3600), api_key="pb_k",
        )
        await prov.revoke_token(rt)
        pool.execute.assert_called_once()
        assert "DELETE" in pool.execute.call_args[0][0]


# ── Combined Verifier ─────────────────────────────────────


class TestCombinedVerifier:
    async def test_api_key_verified_first(self):
        access = AccessToken(token="pb_key", client_id="a1", scopes=["read"])
        verifier = _mock_verifier(returns=access)
        prov, _, _ = _provider()
        combined = CombinedTokenVerifier(verifier, prov)

        result = await combined.verify_token("pb_key")
        assert result is not None
        assert result.client_id == "a1"

    async def test_fallback_to_oauth(self):
        # API key verifier fails for OAuth token, succeeds for underlying key
        prov, _, _ = _provider()
        prov._access_tokens["oauth_tok"] = PbAccessToken(
            token="oauth_tok", client_id="c1", scopes=["read"],
            expires_at=int(time.time() + 3600), api_key="pb_underlying",
        )

        underlying = AccessToken(token="pb_underlying", client_id="agent-1", scopes=["read"])

        call_count = 0

        async def _selective_verify(token):
            nonlocal call_count
            call_count += 1
            if token == "pb_underlying":
                return underlying
            return None

        verifier = AsyncMock()
        verifier.verify_token.side_effect = _selective_verify
        combined = CombinedTokenVerifier(verifier, prov)

        result = await combined.verify_token("oauth_tok")
        assert result is not None
        assert result.client_id == "agent-1"

    async def test_both_fail_returns_none(self):
        prov, _, _ = _provider()
        verifier = _mock_verifier(returns=None)
        combined = CombinedTokenVerifier(verifier, prov)

        result = await combined.verify_token("unknown_token")
        assert result is None


# ── Cleanup ───────────────────────────────────────────────


class TestCleanup:
    async def test_removes_expired_pending_logins(self):
        prov, pool, _ = _provider()
        pool.fetchval.return_value = None  # no refresh tokens to delete
        # Add an expired pending login
        prov._pending_logins["old"] = PendingLogin(
            client=_make_client(), params=_make_params(),
            created_at=time.time() - PENDING_LOGIN_TTL - 10,
        )
        prov._pending_logins["fresh"] = PendingLogin(
            client=_make_client(), params=_make_params(),
        )
        await prov._cleanup()
        assert "old" not in prov._pending_logins
        assert "fresh" in prov._pending_logins

    async def test_removes_expired_auth_codes(self):
        prov, pool, _ = _provider()
        pool.fetchval.return_value = None
        prov._auth_codes["expired"] = PbAuthorizationCode(
            code="expired", client_id="c", redirect_uri="https://x.com/cb",
            redirect_uri_provided_explicitly=True, code_challenge="ch",
            scopes=[], expires_at=time.time() - 10, api_key="pb_k",
        )
        prov._auth_codes["valid"] = PbAuthorizationCode(
            code="valid", client_id="c", redirect_uri="https://x.com/cb",
            redirect_uri_provided_explicitly=True, code_challenge="ch",
            scopes=[], expires_at=time.time() + CODE_TTL, api_key="pb_k",
        )
        await prov._cleanup()
        assert "expired" not in prov._auth_codes
        assert "valid" in prov._auth_codes

    async def test_removes_expired_access_tokens(self):
        prov, pool, _ = _provider()
        pool.fetchval.return_value = None
        prov._access_tokens["old"] = PbAccessToken(
            token="old", client_id="c", scopes=[],
            expires_at=int(time.time() - 10), api_key="pb_k",
        )
        prov._access_tokens["current"] = PbAccessToken(
            token="current", client_id="c", scopes=[],
            expires_at=int(time.time() + 3600), api_key="pb_k",
        )
        await prov._cleanup()
        assert "old" not in prov._access_tokens
        assert "current" in prov._access_tokens


# ── Client Registration ───────────────────────────────────


class TestGetClient:
    async def test_get_existing_client(self):
        prov, pool, _ = _provider()
        client = _make_client("test-client")
        pool.fetchrow.return_value = {"client_info": client.model_dump_json()}

        result = await prov.get_client("test-client")
        assert result is not None
        assert result.client_id == "test-client"

    async def test_get_missing_client(self):
        prov, pool, _ = _provider()
        pool.fetchrow.return_value = None

        result = await prov.get_client("nonexistent")
        assert result is None


class TestRegisterClient:
    async def test_register_new_client(self):
        prov, pool, _ = _provider()
        client = _make_client("new-client")
        await prov.register_client(client)
        pool.execute.assert_called_once()
        call_sql = pool.execute.call_args[0][0]
        assert "INSERT INTO oauth_clients" in call_sql

    async def test_register_upserts_on_conflict(self):
        prov, pool, _ = _provider()
        client = _make_client("existing")
        await prov.register_client(client)
        call_sql = pool.execute.call_args[0][0]
        assert "ON CONFLICT" in call_sql

    async def test_register_db_error_raises(self):
        prov, pool, _ = _provider()
        pool.execute.side_effect = RuntimeError("connection lost")
        with pytest.raises(RuntimeError):
            await prov.register_client(_make_client())
