"""OAuth Authorization Server Provider for Powerbrain MCP.

Implements the MCP SDK's OAuthAuthorizationServerProvider protocol.
Users authenticate by entering their pb_ API key in a login form.
The API key is validated against PostgreSQL and mapped to an OAuth token.

Clients and refresh tokens are persisted in PostgreSQL so that users
don't need to re-authenticate after container restarts.
Access tokens and auth codes remain in-memory (short-lived).
"""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import asyncpg

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
    """OAuth provider that validates pb_ API keys and maps them to OAuth tokens.

    Clients and refresh tokens are persisted in PostgreSQL.
    Access tokens and auth codes are kept in-memory (short-lived).
    """

    def __init__(
        self,
        api_key_verifier: TokenVerifier,
        login_url: str,
        callback_url: str,
        get_pool: "callable",  # async () -> asyncpg.Pool
    ):
        self.api_key_verifier = api_key_verifier
        self.login_url = login_url
        self.callback_url = callback_url
        self._get_pool = get_pool

        # In-memory only (short-lived, OK to lose on restart)
        self._pending_logins: dict[str, PendingLogin] = {}
        self._auth_codes: dict[str, PbAuthorizationCode] = {}
        self._access_tokens: dict[str, PbAccessToken] = {}

        self._cleanup_task: asyncio.Task | None = None

    def start_cleanup(self):
        """Start periodic cleanup of expired entries. Call after event loop is running."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            await self._cleanup()

    async def _cleanup(self):
        now = time.time()
        # In-memory cleanup
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
        # PostgreSQL cleanup: expired refresh tokens
        try:
            pool = await self._get_pool()
            deleted = await pool.fetchval(
                "DELETE FROM oauth_refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < $1 RETURNING count(*)",
                int(now),
            )
            if deleted:
                log.info("OAuth cleanup: removed %s expired refresh tokens", deleted)
        except Exception as e:
            log.warning("OAuth cleanup (refresh tokens) failed: %s", e)

    # ── Client Registration (PostgreSQL) ─────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        try:
            pool = await self._get_pool()
            row = await pool.fetchrow(
                "SELECT client_info FROM oauth_clients WHERE client_id = $1",
                client_id,
            )
            if row is None:
                return None
            return OAuthClientInformationFull.model_validate_json(row["client_info"])
        except Exception as e:
            log.error("Failed to load OAuth client %s: %s", client_id, e)
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(
                """INSERT INTO oauth_clients (client_id, client_secret, client_name,
                        redirect_uris, grant_types, response_types,
                        token_endpoint_auth_method, client_info)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (client_id) DO UPDATE SET
                        client_info = EXCLUDED.client_info""",
                client_info.client_id,
                client_info.client_secret,
                client_info.client_name or "",
                json.dumps([str(u) for u in (client_info.redirect_uris or [])]),
                json.dumps(client_info.grant_types or []),
                json.dumps(client_info.response_types or []),
                client_info.token_endpoint_auth_method or "client_secret_post",
                client_info.model_dump_json(),
            )
            log.info("Registered OAuth client: %s (%s)", client_info.client_id, client_info.client_name)
        except Exception as e:
            log.error("Failed to register OAuth client: %s", e)
            raise

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
            return None, "Login session expired. Please try again."

        if not api_key or not api_key.strip():
            return None, "Please enter an API key."

        api_key = api_key.strip()

        # Validate the API key
        access = await self.api_key_verifier.verify_token(api_key)
        if access is None:
            return None, "Invalid API key. Please check and try again."

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

    # ── Authorization Code (in-memory) ────────────────────

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

        # Access token: in-memory (short-lived)
        self._access_tokens[access_token] = PbAccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL),
            api_key=authorization_code.api_key,
            resource=authorization_code.resource,
        )

        # Refresh token: PostgreSQL (long-lived, survives restarts)
        await self._store_refresh_token(
            token=refresh_token,
            client_id=client.client_id,
            api_key=authorization_code.api_key,
            scopes=authorization_code.scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL),
        )

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # ── Refresh Token (PostgreSQL) ────────────────────────

    async def _store_refresh_token(
        self, token: str, client_id: str, api_key: str,
        scopes: list[str], expires_at: int,
    ) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(
                """INSERT INTO oauth_refresh_tokens (token, client_id, api_key, scopes, expires_at)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (token) DO UPDATE SET
                        expires_at = EXCLUDED.expires_at""",
                token, client_id, api_key,
                json.dumps(scopes), expires_at,
            )
        except Exception as e:
            log.error("Failed to store refresh token: %s", e)
            raise

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> PbRefreshToken | None:
        try:
            pool = await self._get_pool()
            row = await pool.fetchrow(
                "SELECT token, client_id, api_key, scopes, expires_at FROM oauth_refresh_tokens WHERE token = $1",
                refresh_token,
            )
            if row is None:
                return None
            if row["client_id"] != client.client_id:
                return None
            if row["expires_at"] is not None and row["expires_at"] < time.time():
                await pool.execute("DELETE FROM oauth_refresh_tokens WHERE token = $1", refresh_token)
                return None
            scopes = json.loads(row["scopes"]) if row["scopes"] else []
            return PbRefreshToken(
                token=row["token"],
                client_id=row["client_id"],
                scopes=scopes,
                expires_at=row["expires_at"],
                api_key=row["api_key"],
            )
        except Exception as e:
            log.error("Failed to load refresh token: %s", e)
            return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: PbRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = time.time()
        new_access_token = secrets.token_urlsafe(32)

        # New access token: in-memory
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

    # ── Access Token Verification (in-memory) ─────────────

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
            try:
                pool = await self._get_pool()
                await pool.execute("DELETE FROM oauth_refresh_tokens WHERE token = $1", token.token)
            except Exception as e:
                log.warning("Failed to revoke refresh token from DB: %s", e)


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
