"""OAuth Authorization Server Provider for Powerbrain MCP.

Implements the MCP SDK's OAuthAuthorizationServerProvider protocol.
Users authenticate by entering their pb_ API key in a login form.
The API key is validated against PostgreSQL and mapped to an OAuth token.
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    TokenVerifier,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from login_page import render_login_page

log = logging.getLogger("pb-mcp")

# ── TTLs ──────────────────────────────────────────────────
PENDING_LOGIN_TTL = 10 * 60  # 10 minutes
CODE_TTL = 5 * 60  # 5 minutes
ACCESS_TOKEN_TTL = 60 * 60  # 1 hour
REFRESH_TOKEN_TTL = 7 * 24 * 60 * 60  # 7 days
CLEANUP_INTERVAL = 5 * 60  # 5 minutes


# ── Extended models with api_key ──────────────────────────

class PbAuthorizationCode(AuthorizationCode):
    api_key: str  # The validated pb_ API key


class PbAccessToken(AccessToken):
    api_key: str  # The pb_ API key mapped to this token


class PbRefreshToken(RefreshToken):
    api_key: str  # The pb_ API key mapped to this token


@dataclass
class PendingLogin:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float = field(default_factory=time.time)


class PowerbrainOAuthProvider(
    OAuthAuthorizationServerProvider[PbAuthorizationCode, PbRefreshToken, PbAccessToken]
):
    """OAuth provider that validates pb_ API keys and maps them to OAuth tokens."""

    def __init__(
        self,
        api_key_verifier: TokenVerifier,
        login_url: str,
        callback_url: str,
    ):
        self.api_key_verifier = api_key_verifier
        self.login_url = login_url
        self.callback_url = callback_url

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending_logins: dict[str, PendingLogin] = {}
        self._auth_codes: dict[str, PbAuthorizationCode] = {}
        self._access_tokens: dict[str, PbAccessToken] = {}
        self._refresh_tokens: dict[str, PbRefreshToken] = {}

        self._cleanup_task: asyncio.Task | None = None

    def start_cleanup(self):
        """Start periodic cleanup of expired entries. Call after event loop is running."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            self._cleanup()

    def _cleanup(self):
        now = time.time()
        self._pending_logins = {
            k: v for k, v in self._pending_logins.items()
            if now - v.created_at < PENDING_LOGIN_TTL
        }
        self._auth_codes = {
            k: v for k, v in self._auth_codes.items()
            if v.expires_at > now
        }
        self._access_tokens = {
            k: v for k, v in self._access_tokens.items()
            if v.expires_at is None or v.expires_at > now
        }
        self._refresh_tokens = {
            k: v for k, v in self._refresh_tokens.items()
            if v.expires_at is None or v.expires_at > now
        }

    # ── Client Registration ───────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    # ── Authorization ─────────────────────────────────────

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Store pending login and redirect to login form."""
        login_session_id = secrets.token_urlsafe(32)
        self._pending_logins[login_session_id] = PendingLogin(
            client=client, params=params,
        )

        # Redirect to our login form
        query = urlencode({"session": login_session_id})
        return f"{self.login_url}?{query}"

    async def handle_login_callback(
        self,
        login_session_id: str,
        api_key: str,
    ) -> tuple[str | None, str | None]:
        """Validate API key and return (redirect_url, error).

        Returns:
            (redirect_url, None) on success — redirect to client with auth code
            (None, error_message) on failure — re-show login form
        """
        pending = self._pending_logins.get(login_session_id)
        if not pending:
            return None, "Login-Session abgelaufen. Bitte erneut versuchen."

        if not api_key or not api_key.strip():
            return None, "Bitte einen API-Key eingeben."

        api_key = api_key.strip()

        # Validate the API key
        access = await self.api_key_verifier.verify_token(api_key)
        if access is None:
            return None, "Ungültiger API-Key. Bitte prüfen und erneut versuchen."

        # Valid! Generate auth code
        del self._pending_logins[login_session_id]

        code = secrets.token_urlsafe(32)
        now = time.time()

        self._auth_codes[code] = PbAuthorizationCode(
            code=code,
            client_id=pending.client.client_id,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            code_challenge=pending.params.code_challenge,
            scopes=pending.params.scopes or [],
            expires_at=now + CODE_TTL,
            api_key=api_key,
            resource=pending.params.resource,
        )

        redirect_url = construct_redirect_uri(
            str(pending.params.redirect_uri),
            code=code,
            state=pending.params.state,
        )
        return redirect_url, None

    # ── Authorization Code ────────────────────────────────

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> PbAuthorizationCode | None:
        code_data = self._auth_codes.get(authorization_code)
        if code_data is None:
            return None
        if code_data.client_id != client.client_id:
            return None
        if code_data.expires_at < time.time():
            del self._auth_codes[authorization_code]
            return None
        return code_data

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: PbAuthorizationCode,
    ) -> OAuthToken:
        # Remove used code
        self._auth_codes.pop(authorization_code.code, None)

        now = time.time()
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)

        self._access_tokens[access_token] = PbAccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL),
            api_key=authorization_code.api_key,
            resource=authorization_code.resource,
        )

        self._refresh_tokens[refresh_token] = PbRefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL),
            api_key=authorization_code.api_key,
        )

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # ── Refresh Token ─────────────────────────────────────

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> PbRefreshToken | None:
        token_data = self._refresh_tokens.get(refresh_token)
        if token_data is None:
            return None
        if token_data.client_id != client.client_id:
            return None
        if token_data.expires_at is not None and token_data.expires_at < time.time():
            del self._refresh_tokens[refresh_token]
            return None
        return token_data

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: PbRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = time.time()
        new_access_token = secrets.token_urlsafe(32)

        self._access_tokens[new_access_token] = PbAccessToken(
            token=new_access_token,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL),
            api_key=refresh_token.api_key,
        )

        return OAuthToken(
            access_token=new_access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token.token,
            scope=" ".join(scopes) if scopes else None,
        )

    # ── Access Token Verification ─────────────────────────

    async def load_access_token(self, token: str) -> PbAccessToken | None:
        token_data = self._access_tokens.get(token)
        if token_data is None:
            return None
        if token_data.expires_at is not None and token_data.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return token_data

    # ── Token Revocation ──────────────────────────────────

    async def revoke_token(self, token: PbAccessToken | PbRefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        if isinstance(token, PbRefreshToken):
            self._refresh_tokens.pop(token.token, None)


class CombinedTokenVerifier(TokenVerifier):
    """Tries API key verification first, then falls back to OAuth token."""

    def __init__(
        self,
        api_key_verifier: TokenVerifier,
        oauth_provider: PowerbrainOAuthProvider,
    ):
        self.api_key_verifier = api_key_verifier
        self.oauth_provider = oauth_provider

    async def verify_token(self, token: str) -> AccessToken | None:
        # Try API key first (pb_ prefix)
        result = await self.api_key_verifier.verify_token(token)
        if result is not None:
            return result

        # Fall back to OAuth token
        oauth_token = await self.oauth_provider.load_access_token(token)
        if oauth_token is not None:
            # Re-validate the underlying API key to get agent_id/role
            api_result = await self.api_key_verifier.verify_token(oauth_token.api_key)
            if api_result is not None:
                return AccessToken(
                    token=token,
                    client_id=api_result.client_id,
                    scopes=api_result.scopes,
                    expires_at=oauth_token.expires_at,
                )
        return None
