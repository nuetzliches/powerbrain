-- 010_api_keys.sql: API-Key-Authentifizierung
-- Speichert gehashte API-Keys mit Rollen-Mapping

CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash      TEXT NOT NULL UNIQUE,
    agent_id      TEXT NOT NULL UNIQUE,
    agent_role    TEXT NOT NULL DEFAULT 'analyst'
                  CHECK (agent_role IN ('analyst', 'developer', 'admin')),
    description   TEXT,
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);

-- Grant permissions for the MCP app user
GRANT SELECT, UPDATE ON api_keys TO mcp_app;

-- Default development key (only for local development!)
-- Key: kb_dev_localonly_do_not_use_in_production
-- Hash: SHA-256 of the above
INSERT INTO api_keys (key_hash, agent_id, agent_role, description)
VALUES (
    encode(sha256('kb_dev_localonly_do_not_use_in_production'::bytea), 'hex'),
    'dev-agent',
    'admin',
    'Default development key — DO NOT use in production'
)
ON CONFLICT (agent_id) DO NOTHING;
