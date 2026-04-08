-- ============================================================
-- 014_audit_hashchain.sql — Tamper-Resistant Audit Logs (EU AI Act Art. 12)
-- ============================================================
-- Adds a SHA-256 hash chain to agent_access_log:
--   - prev_hash + entry_hash columns on each row
--   - BEFORE INSERT trigger computes hash transparently
--   - pg_advisory_xact_lock serializes only audit writes
--   - pb_verify_audit_chain() verifies full or partial chain
--   - audit_archive table stores checkpoints before retention-pruning
--   - pb_audit_checkpoint_and_prune() prunes old rows while
--     keeping the chain mathematically continuous via checkpoint
-- ============================================================

-- pgcrypto provides digest() for SHA-256
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
-- Hash-chain columns on agent_access_log
-- ------------------------------------------------------------
ALTER TABLE agent_access_log
    ADD COLUMN IF NOT EXISTS prev_hash  BYTEA,
    ADD COLUMN IF NOT EXISTS entry_hash BYTEA;

-- ------------------------------------------------------------
-- audit_archive: checkpoint registry for retention cleanup
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_archive (
    id                 BIGSERIAL PRIMARY KEY,
    archived_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_entry_id      BIGINT NOT NULL,
    last_verified_hash BYTEA  NOT NULL,
    row_count          BIGINT NOT NULL,
    chain_valid        BOOLEAN NOT NULL,
    first_invalid_id   BIGINT,
    retention_cutoff   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_archive_archived_at ON audit_archive(archived_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_archive_last_entry_id ON audit_archive(last_entry_id DESC);

-- RLS: mcp_auditor can read archive, no role can modify directly
ALTER TABLE audit_archive ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_archive FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_auditor') THEN
        GRANT SELECT ON audit_archive TO mcp_auditor;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename  = 'audit_archive'
              AND policyname = 'audit_archive_read_only'
        ) THEN
            EXECUTE 'CREATE POLICY audit_archive_read_only ON audit_archive '
                 || 'FOR SELECT TO mcp_auditor USING (true)';
        END IF;
    END IF;
END
$$;

-- ------------------------------------------------------------
-- Canonical payload for hashing (column-order-independent, NULL-safe)
-- ------------------------------------------------------------
-- Kept as a helper function so trigger and verify use identical input.
CREATE OR REPLACE FUNCTION pb_audit_hash_payload(
    p_prev            BYTEA,
    p_id              BIGINT,
    p_agent_id        TEXT,
    p_agent_role      TEXT,
    p_resource_type   TEXT,
    p_resource_id     TEXT,
    p_action          TEXT,
    p_policy_result   TEXT,
    p_policy_reason   TEXT,
    p_request_ctx     JSONB,
    p_created_at      TIMESTAMPTZ,
    p_contains_pii    BOOLEAN,
    p_purpose         TEXT,
    p_legal_basis     TEXT,
    p_data_category   TEXT,
    p_fields_redacted TEXT[]
) RETURNS BYTEA
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT digest(
        COALESCE(p_prev, '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA)
        || convert_to(p_id::TEXT,                              'UTF8')
        || convert_to(COALESCE(p_agent_id, ''),                'UTF8')
        || convert_to(COALESCE(p_agent_role, ''),              'UTF8')
        || convert_to(COALESCE(p_resource_type, ''),           'UTF8')
        || convert_to(COALESCE(p_resource_id, ''),             'UTF8')
        || convert_to(COALESCE(p_action, ''),                  'UTF8')
        || convert_to(COALESCE(p_policy_result, ''),           'UTF8')
        || convert_to(COALESCE(p_policy_reason, ''),           'UTF8')
        || convert_to(COALESCE(p_request_ctx::TEXT, ''),       'UTF8')
        || convert_to(to_char(p_created_at AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US'),   'UTF8')
        || convert_to(CASE WHEN p_contains_pii THEN '1' ELSE '0' END, 'UTF8')
        || convert_to(COALESCE(p_purpose, ''),                 'UTF8')
        || convert_to(COALESCE(p_legal_basis, ''),             'UTF8')
        || convert_to(COALESCE(p_data_category, ''),           'UTF8')
        || convert_to(COALESCE(array_to_string(p_fields_redacted, ',', '?'), ''), 'UTF8'),
        'sha256'
    );
$$;

-- ------------------------------------------------------------
-- BEFORE INSERT trigger: compute prev_hash + entry_hash
-- ------------------------------------------------------------
-- Advisory lock id 847291 (documented in opa-policies/pb/data.json)
-- serializes audit writes only; other transactions stay parallel.
CREATE OR REPLACE FUNCTION pb_audit_hashchain_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_prev BYTEA;
BEGIN
    -- Serialize only audit-chain writes
    PERFORM pg_advisory_xact_lock(847291);

    -- Tail of the chain: last row OR last archive checkpoint OR genesis
    SELECT entry_hash INTO v_prev
        FROM agent_access_log
        ORDER BY id DESC
        LIMIT 1;

    IF v_prev IS NULL THEN
        SELECT last_verified_hash INTO v_prev
            FROM audit_archive
            ORDER BY archived_at DESC
            LIMIT 1;
    END IF;

    IF v_prev IS NULL THEN
        v_prev := '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA;
    END IF;

    NEW.prev_hash  := v_prev;
    NEW.entry_hash := pb_audit_hash_payload(
        v_prev,
        NEW.id,
        NEW.agent_id,
        NEW.agent_role,
        NEW.resource_type,
        NEW.resource_id,
        NEW.action,
        NEW.policy_result,
        NEW.policy_reason,
        NEW.request_context,
        NEW.created_at,
        NEW.contains_pii,
        NEW.purpose,
        NEW.legal_basis,
        NEW.data_category,
        NEW.fields_redacted
    );

    RETURN NEW;
END;
$$;

-- Drop and recreate to be idempotent
DROP TRIGGER IF EXISTS trg_audit_hashchain ON agent_access_log;
CREATE TRIGGER trg_audit_hashchain
    BEFORE INSERT ON agent_access_log
    FOR EACH ROW
    EXECUTE FUNCTION pb_audit_hashchain_trigger();

-- ------------------------------------------------------------
-- Append-only enforcement: reject any UPDATE to prevent chain tampering.
-- DELETE remains allowed (gated by privilege + used by checkpoint-prune).
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION pb_audit_block_update_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'agent_access_log is append-only — UPDATE is not permitted (row id=%). '
                    'If anonymization is required, append a redaction event row instead.',
                    OLD.id
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_block_update ON agent_access_log;
CREATE TRIGGER trg_audit_block_update
    BEFORE UPDATE ON agent_access_log
    FOR EACH ROW
    EXECUTE FUNCTION pb_audit_block_update_trigger();

-- ------------------------------------------------------------
-- pb_verify_audit_chain(start_id, end_id)
--   Verifies the hash chain over the given ID range.
--   NULL bounds mean "from beginning" / "to current tail".
--   The starting prev_hash is resolved from:
--     1. entry_hash of (p_start_id - 1) if present
--     2. latest audit_archive.last_verified_hash
--     3. genesis (32 zero bytes)
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION pb_verify_audit_chain(
    p_start_id BIGINT DEFAULT NULL,
    p_end_id   BIGINT DEFAULT NULL
) RETURNS TABLE(
    valid            BOOLEAN,
    first_invalid_id BIGINT,
    total_checked    BIGINT,
    last_valid_hash  BYTEA
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_expected_prev BYTEA;
    v_row           agent_access_log%ROWTYPE;
    v_count         BIGINT := 0;
    v_computed      BYTEA;
    v_last_valid    BYTEA;
BEGIN
    -- Resolve starting prev_hash
    IF p_start_id IS NULL OR p_start_id <= 1 THEN
        SELECT last_verified_hash INTO v_expected_prev
            FROM audit_archive
            ORDER BY archived_at DESC
            LIMIT 1;
    ELSE
        SELECT entry_hash INTO v_expected_prev
            FROM agent_access_log
            WHERE id = p_start_id - 1;
        IF v_expected_prev IS NULL THEN
            SELECT last_verified_hash INTO v_expected_prev
                FROM audit_archive
                ORDER BY archived_at DESC
                LIMIT 1;
        END IF;
    END IF;

    IF v_expected_prev IS NULL THEN
        v_expected_prev := '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA;
    END IF;

    v_last_valid := v_expected_prev;

    FOR v_row IN
        SELECT * FROM agent_access_log
        WHERE (p_start_id IS NULL OR id >= p_start_id)
          AND (p_end_id   IS NULL OR id <= p_end_id)
        ORDER BY id ASC
    LOOP
        v_computed := pb_audit_hash_payload(
            v_expected_prev,
            v_row.id,
            v_row.agent_id,
            v_row.agent_role,
            v_row.resource_type,
            v_row.resource_id,
            v_row.action,
            v_row.policy_result,
            v_row.policy_reason,
            v_row.request_context,
            v_row.created_at,
            v_row.contains_pii,
            v_row.purpose,
            v_row.legal_basis,
            v_row.data_category,
            v_row.fields_redacted
        );

        IF v_row.prev_hash  IS DISTINCT FROM v_expected_prev
           OR v_row.entry_hash IS DISTINCT FROM v_computed THEN
            RETURN QUERY SELECT FALSE, v_row.id, v_count, v_last_valid;
            RETURN;
        END IF;

        v_last_valid    := v_row.entry_hash;
        v_expected_prev := v_row.entry_hash;
        v_count         := v_count + 1;
    END LOOP;

    RETURN QUERY SELECT TRUE, NULL::BIGINT, v_count, v_last_valid;
END;
$$;

-- ------------------------------------------------------------
-- pb_audit_checkpoint_and_prune(retention_days)
--   1. Finds last row older than cutoff
--   2. Verifies chain up to that row
--   3. If valid: writes checkpoint to audit_archive, deletes rows
--   4. If invalid: writes archive entry with chain_valid=false,
--      does NOT delete (fail-closed)
--   Returns a one-row summary.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION pb_audit_checkpoint_and_prune(
    p_retention_days INT DEFAULT 365
) RETURNS TABLE(
    checkpoint_id    BIGINT,
    last_entry_id    BIGINT,
    row_count        BIGINT,
    deleted_count    BIGINT,
    chain_valid      BOOLEAN,
    first_invalid_id BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_cutoff       TIMESTAMPTZ;
    v_last_id      BIGINT;
    v_last_hash    BYTEA;
    v_count        BIGINT;
    v_deleted      BIGINT := 0;
    v_verify       RECORD;
    v_checkpoint   BIGINT;
BEGIN
    IF p_retention_days IS NULL OR p_retention_days < 1 THEN
        RAISE EXCEPTION 'retention_days must be >= 1, got %', p_retention_days;
    END IF;

    v_cutoff := now() - (p_retention_days || ' days')::INTERVAL;

    -- Take audit lock so concurrent writes don't race the checkpoint
    PERFORM pg_advisory_xact_lock(847291);

    SELECT id, entry_hash INTO v_last_id, v_last_hash
        FROM agent_access_log
        WHERE created_at < v_cutoff
        ORDER BY id DESC
        LIMIT 1;

    IF v_last_id IS NULL THEN
        RETURN QUERY SELECT NULL::BIGINT, NULL::BIGINT, 0::BIGINT, 0::BIGINT, TRUE, NULL::BIGINT;
        RETURN;
    END IF;

    SELECT * INTO v_verify FROM pb_verify_audit_chain(NULL, v_last_id);

    SELECT COUNT(*) INTO v_count
        FROM agent_access_log
        WHERE id <= v_last_id;

    INSERT INTO audit_archive(
        archived_at, last_entry_id, last_verified_hash,
        row_count, chain_valid, first_invalid_id, retention_cutoff
    )
    VALUES (
        now(), v_last_id, v_last_hash,
        v_count, v_verify.valid, v_verify.first_invalid_id, v_cutoff
    )
    RETURNING id INTO v_checkpoint;

    IF v_verify.valid THEN
        DELETE FROM agent_access_log WHERE id <= v_last_id;
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
    END IF;

    RETURN QUERY SELECT
        v_checkpoint, v_last_id, v_count, v_deleted,
        v_verify.valid, v_verify.first_invalid_id;
END;
$$;

-- ------------------------------------------------------------
-- Backfill: existing rows (if any) get prev_hash / entry_hash
-- so the chain starts from the genesis and is immediately verifiable.
-- For a fresh database this is a no-op.
-- ------------------------------------------------------------
DO $$
DECLARE
    v_row           agent_access_log%ROWTYPE;
    v_expected_prev BYTEA := '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA;
    v_hash          BYTEA;
BEGIN
    IF EXISTS (SELECT 1 FROM agent_access_log WHERE entry_hash IS NULL LIMIT 1) THEN
        FOR v_row IN SELECT * FROM agent_access_log ORDER BY id ASC LOOP
            v_hash := pb_audit_hash_payload(
                v_expected_prev,
                v_row.id,
                v_row.agent_id,
                v_row.agent_role,
                v_row.resource_type,
                v_row.resource_id,
                v_row.action,
                v_row.policy_result,
                v_row.policy_reason,
                v_row.request_context,
                v_row.created_at,
                v_row.contains_pii,
                v_row.purpose,
                v_row.legal_basis,
                v_row.data_category,
                v_row.fields_redacted
            );
            UPDATE agent_access_log
                SET prev_hash = v_expected_prev,
                    entry_hash = v_hash
                WHERE id = v_row.id;
            v_expected_prev := v_hash;
        END LOOP;
    END IF;
END
$$;

-- ------------------------------------------------------------
-- pb_verify_audit_chain_tail(n)
--   Lightweight chain verifier that only checks the last N rows.
--   Used by the /health risk-indicator endpoint so a JSON health
--   request does not trigger an O(table_size) scan on every poll.
--   Note: this is a "boundary check" — it verifies that the last N
--   rows form a self-consistent prefix-anchored chain. A break in
--   older rows is detected by the daily audit_retention_cleanup job
--   in pb-worker.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION pb_verify_audit_chain_tail(
    p_max_rows BIGINT DEFAULT 1000
) RETURNS TABLE(
    valid            BOOLEAN,
    first_invalid_id BIGINT,
    total_checked    BIGINT,
    last_valid_hash  BYTEA
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_start BIGINT;
BEGIN
    SELECT MAX(id) - p_max_rows + 1 INTO v_start FROM agent_access_log;
    IF v_start IS NULL OR v_start < 1 THEN
        v_start := NULL;  -- Verify whole chain when it is shorter than the cap
    END IF;
    RETURN QUERY SELECT * FROM pb_verify_audit_chain(v_start, NULL);
END;
$$;

COMMENT ON COLUMN agent_access_log.prev_hash  IS 'SHA-256 of the previous audit entry (hash chain, EU AI Act Art. 12)';
COMMENT ON COLUMN agent_access_log.entry_hash IS 'SHA-256 of this audit entry including prev_hash';
COMMENT ON TABLE  audit_archive               IS 'Checkpoint registry for audit-log retention cleanup (EU AI Act Art. 12 + DSGVO retention)';
