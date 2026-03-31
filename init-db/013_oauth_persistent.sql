-- 013_oauth_persistent.sql: Persistent OAuth client/token storage
-- Survives container restarts so Claude.ai users don't need to re-authenticate.

-- Registered OAuth clients (one per user session)
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id       TEXT PRIMARY KEY,
    client_secret   TEXT,
    client_name     TEXT,
    redirect_uris   JSONB NOT NULL DEFAULT '[]',
    grant_types     JSONB NOT NULL DEFAULT '["authorization_code","refresh_token"]',
    response_types  JSONB NOT NULL DEFAULT '["code"]',
    token_endpoint_auth_method TEXT DEFAULT 'client_secret_post',
    client_info     JSONB NOT NULL,          -- Full OAuthClientInformationFull as JSON
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- OAuth refresh tokens (long-lived, 7 days)
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token           TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    api_key         TEXT NOT NULL,            -- The pb_ API key mapped to this token
    scopes          JSONB NOT NULL DEFAULT '[]',
    expires_at      BIGINT,                  -- Unix timestamp
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_oauth_refresh_client ON oauth_refresh_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_expires ON oauth_refresh_tokens(expires_at);

-- Grant permissions for the MCP app user
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_clients TO mcp_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_refresh_tokens TO mcp_app;
    END IF;
END
$$;
