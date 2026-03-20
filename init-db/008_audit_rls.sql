-- ============================================================
-- 008_audit_rls.sql — Row-Level Security for Audit Logs
-- ============================================================
-- Secures agent_access_log so that:
--   - mcp_app can INSERT (write audit entries) but not read/modify
--   - mcp_auditor can SELECT (compliance/monitoring) but not modify
--   - No role can UPDATE or DELETE (append-only audit trail)
-- ============================================================

-- Create auditor role (for compliance / monitoring access)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_auditor') THEN
        CREATE ROLE mcp_auditor NOLOGIN;
    END IF;
END
$$;

-- Grant connect + usage so mcp_auditor can query the table
GRANT CONNECT ON DATABASE knowledgebase TO mcp_auditor;
GRANT USAGE ON SCHEMA public TO mcp_auditor;

-- Enable RLS
ALTER TABLE agent_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_access_log FORCE ROW LEVEL SECURITY;

-- Policy: mcp_app can only INSERT (write audit entries)
CREATE POLICY audit_insert_only ON agent_access_log
    FOR INSERT TO mcp_app
    WITH CHECK (true);

-- Policy: mcp_auditor can only SELECT (read for compliance)
CREATE POLICY audit_read_only ON agent_access_log
    FOR SELECT TO mcp_auditor
    USING (true);

-- Explicit: mcp_app gets INSERT, mcp_auditor gets SELECT
GRANT INSERT ON agent_access_log TO mcp_app;
GRANT SELECT ON agent_access_log TO mcp_auditor;

-- Ensure sequence access for mcp_app (BIGSERIAL needs it)
GRANT USAGE, SELECT ON SEQUENCE agent_access_log_id_seq TO mcp_app;
