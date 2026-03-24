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

-- Grant permissions for the MCP app user (conditional — mcp_app may not exist yet)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_app') THEN
        GRANT SELECT, UPDATE ON api_keys TO mcp_app;
    END IF;
END
$$;

-- Default development key (only for local development!)
-- Key: pb_dev_localonly_do_not_use_in_production
-- Hash: SHA-256 of the above
INSERT INTO api_keys (key_hash, agent_id, agent_role, description)
VALUES (
    '3eb7305e7f42d3a7645906d042ef0c57433388abeb2105031bd33d3cc0e51919',
    'dev-agent',
    'admin',
    'Default development key — DO NOT use in production'
)
ON CONFLICT (agent_id) DO NOTHING;
