-- Graph sync log for tracking knowledge graph mutations
CREATE TABLE IF NOT EXISTS graph_sync_log (
    id          SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    details     JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_graph_sync_log_entity
    ON graph_sync_log (entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_graph_sync_log_created
    ON graph_sync_log (created_at);

-- Grant permissions for the MCP app user (conditional — mcp_app may not exist yet)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_app') THEN
        GRANT INSERT, SELECT ON graph_sync_log TO mcp_app;
        GRANT USAGE, SELECT ON SEQUENCE graph_sync_log_id_seq TO mcp_app;
    END IF;
END
$$;
