-- ============================================================
-- 024_audit_integrity_status.sql — Cache for the transparency
-- report's audit_integrity field (#95).
-- ============================================================
-- Background:
--   _transparency_audit_snapshot() in mcp-server runs
--   pb_verify_audit_chain() inside the get_system_info request
--   handler. The request's own audit-log INSERT happens AFTER the
--   handler returns, so the verifier always sees committed state
--   from BEFORE the request — total_checked underreports by 1
--   relative to the chain length immediately after the request
--   returns. This made get_system_info inconsistent with a follow-up
--   verify_audit_integrity call on the same DB at the same instant.
--
-- Fix:
--   Decouple the snapshot from the request path. A new single-row
--   table stores the most recent verifier result. The pb-worker
--   refreshes it periodically (~ every 60 s by default). The server
--   reads this cache → snapshot reflects committed state independent
--   of the current request, and consumers see a `checked_at`
--   timestamp so they can judge staleness themselves.
--   Live answers remain available through the
--   verify_audit_integrity MCP tool.
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_integrity_status (
    id               INT          PRIMARY KEY CHECK (id = 1),
    valid            BOOLEAN,
    total_checked    BIGINT       NOT NULL DEFAULT 0,
    first_invalid_id BIGINT,
    last_valid_hash  BYTEA,
    checked_at       TIMESTAMPTZ,
    error            TEXT,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE  audit_integrity_status IS
    'Single-row cache holding the most recent pb_verify_audit_chain_tail() result. Refreshed periodically by the pb-worker job audit_integrity_status_refresh (~60 s). Read by mcp-server _transparency_audit_snapshot() to decouple the snapshot from the request path (issue #95).';
COMMENT ON COLUMN audit_integrity_status.valid            IS 'TRUE/FALSE result of the last pb_verify_audit_chain_tail() call; NULL if the worker has not run yet or the last call errored.';
COMMENT ON COLUMN audit_integrity_status.total_checked    IS 'Rows verified during the last refresh (capped at AUDIT_INTEGRITY_TAIL_ROWS).';
COMMENT ON COLUMN audit_integrity_status.first_invalid_id IS 'id of the first row that failed verification, NULL if chain is valid.';
COMMENT ON COLUMN audit_integrity_status.last_valid_hash  IS 'Hash up to which the chain has been verified (entry_hash of the last validated row, or the seed if the verified range was empty).';
COMMENT ON COLUMN audit_integrity_status.checked_at       IS 'Timestamp at which the last successful refresh ran. Consumers should compare with now() to assess staleness.';
COMMENT ON COLUMN audit_integrity_status.error            IS 'Last refresh-error message (truncated to 500 chars), NULL on success.';

-- RLS: only the worker (DB owner / superuser) writes; mcp_auditor reads.
ALTER TABLE audit_integrity_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_integrity_status FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_auditor') THEN
        GRANT SELECT ON audit_integrity_status TO mcp_auditor;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename  = 'audit_integrity_status'
              AND policyname = 'audit_integrity_status_read_only'
        ) THEN
            EXECUTE 'CREATE POLICY audit_integrity_status_read_only ON audit_integrity_status '
                 || 'FOR SELECT TO mcp_auditor USING (true)';
        END IF;
    END IF;
END
$$;
