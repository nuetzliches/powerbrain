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
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_app') THEN
        CREATE ROLE mcp_app NOLOGIN;
    END IF;
END
$$;

-- Grant connect + usage so mcp_auditor can query the table.
-- The database name comes from the connection rather than a literal: this
-- used to hard-code "powerbrain", which has never existed on this deployment
-- (the database is "knowledgebase"), so the GRANT failed on every single run.
-- ON_ERROR_STOP=0 swallowed it, leaving mcp_auditor without CONNECT.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO mcp_auditor', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO mcp_auditor;

-- Enable RLS
ALTER TABLE agent_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_access_log FORCE ROW LEVEL SECURITY;

-- Policies, guarded against pg_policies the same way 014/022/024 do it
-- (Postgres has no CREATE POLICY IF NOT EXISTS):
--   - mcp_app can only INSERT (write audit entries)
--   - mcp_auditor can only SELECT (read for compliance)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename  = 'agent_access_log'
          AND policyname = 'audit_insert_only'
    ) THEN
        EXECUTE 'CREATE POLICY audit_insert_only ON agent_access_log '
             || 'FOR INSERT TO mcp_app WITH CHECK (true)';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename  = 'agent_access_log'
          AND policyname = 'audit_read_only'
    ) THEN
        EXECUTE 'CREATE POLICY audit_read_only ON agent_access_log '
             || 'FOR SELECT TO mcp_auditor USING (true)';
    END IF;
END
$$;

-- Explicit: mcp_app gets INSERT, mcp_auditor gets SELECT
GRANT INSERT ON agent_access_log TO mcp_app;
GRANT SELECT ON agent_access_log TO mcp_auditor;

-- Ensure sequence access for mcp_app (BIGSERIAL needs it)
GRANT USAGE, SELECT ON SEQUENCE agent_access_log_id_seq TO mcp_app;
