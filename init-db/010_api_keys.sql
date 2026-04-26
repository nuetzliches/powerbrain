-- 010_api_keys.sql: API key authentication
-- Stores hashed API keys with role mapping

CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash      TEXT NOT NULL UNIQUE,
    agent_id      TEXT NOT NULL UNIQUE,
    agent_role    TEXT NOT NULL DEFAULT 'analyst'
                  CHECK (agent_role IN ('viewer', 'analyst', 'developer', 'admin')),
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

-- Demo keys for the sales-demo UI (docker compose --profile demo)
-- Key: pb_demo_analyst_localonly — role analyst (sees public/internal/confidential)
-- Key: pb_demo_viewer_localonly  — role viewer  (sees public only)
-- Both are safe only on local/demo machines. Never enable in production.
INSERT INTO api_keys (key_hash, agent_id, agent_role, description)
VALUES (
    'da9884aad20814b700683b9780603734ded1980030c08488e5c4a7c3fac37f9a',
    'demo-analyst',
    'analyst',
    'Sales-demo analyst key — DO NOT use in production'
)
ON CONFLICT (agent_id) DO NOTHING;

INSERT INTO api_keys (key_hash, agent_id, agent_role, description)
VALUES (
    '4ffba85aded3963ce4f8320cf611fc57d6abbbf361227289274326f449acc4cc',
    'demo-viewer',
    'viewer',
    'Sales-demo viewer key — DO NOT use in production'
)
ON CONFLICT (agent_id) DO NOTHING;
