-- ============================================================
-- 022_audit_tail_pointer.sql — Serialize hash-chain writes (#59)
-- ============================================================
-- Replaces the "advisory lock + SELECT ORDER BY id DESC LIMIT 1"
-- pattern used by 014_audit_hashchain.sql's BEFORE INSERT trigger.
--
-- Why the old pattern breaks under concurrency:
--   The advisory lock (pg_advisory_xact_lock) serializes trigger
--   execution, but a BEFORE INSERT trigger inherits the parent
--   INSERT statement's READ COMMITTED snapshot — taken *before*
--   the lock was acquired. Two concurrent writers therefore both
--   observe the same tail entry_hash, both compute
--   sha256(tail || payload), and both insert with the SAME
--   prev_hash. pb_verify_audit_chain() then reports valid=false.
--
-- How the new pattern fixes it:
--   A single-row audit_tail table is protected by SELECT ... FOR
--   UPDATE. Row-level locks have stricter visibility semantics in
--   READ COMMITTED: after the lock is granted, Postgres re-reads
--   the committed version of the row — not the statement snapshot
--   value. The second writer therefore sees the first writer's
--   committed hash and chains correctly.
--
-- Compatibility:
--   * pb_verify_audit_chain() and pb_verify_audit_chain_tail() are
--     unchanged — they verify inter-row hash linkage, independent
--     of the tail table.
--   * pb_audit_checkpoint_and_prune() is rewritten below to take
--     the same row-level lock instead of the advisory lock.
--   * Existing audit rows are preserved; audit_tail is seeded from
--     the current tail or from the genesis hash.
-- ============================================================

-- ------------------------------------------------------------
-- audit_tail: single-row pointer protected by row-level locks
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_tail (
    id              INT         PRIMARY KEY CHECK (id = 1),
    last_entry_hash BYTEA       NOT NULL,
    last_entry_id   BIGINT      NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  audit_tail IS 'Single-row tail pointer for the agent_access_log hash chain. Updated atomically via SELECT ... FOR UPDATE to serialize concurrent audit writers (see issue #59).';
COMMENT ON COLUMN audit_tail.last_entry_hash IS 'SHA-256 entry_hash of the most recently inserted agent_access_log row. Used as prev_hash for the next row.';
COMMENT ON COLUMN audit_tail.last_entry_id   IS 'id of the most recently inserted agent_access_log row. Assigned atomically by the hash-chain trigger (overrides the BIGSERIAL default) so id order matches chain order under concurrency.';

-- Seed with whichever is available first:
--   1. The existing tail (entry_hash of the newest agent_access_log row)
--   2. The most recent audit_archive checkpoint hash
--   3. The genesis (32 zero bytes)
INSERT INTO audit_tail (id, last_entry_hash, last_entry_id)
SELECT 1,
       COALESCE(
           (SELECT entry_hash FROM agent_access_log ORDER BY id DESC LIMIT 1),
           (SELECT last_verified_hash FROM audit_archive ORDER BY archived_at DESC LIMIT 1),
           '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA
       ),
       COALESCE((SELECT id FROM agent_access_log ORDER BY id DESC LIMIT 1), 0)
ON CONFLICT (id) DO NOTHING;

-- RLS: only the audit trigger function (SECURITY DEFINER) writes here.
-- mcp_auditor may read for transparency; nothing else may touch it.
ALTER TABLE audit_tail ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_tail FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_auditor') THEN
        GRANT SELECT ON audit_tail TO mcp_auditor;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename  = 'audit_tail'
              AND policyname = 'audit_tail_read_only'
        ) THEN
            EXECUTE 'CREATE POLICY audit_tail_read_only ON audit_tail '
                 || 'FOR SELECT TO mcp_auditor USING (true)';
        END IF;
    END IF;
END
$$;

-- ------------------------------------------------------------
-- Rewrite BEFORE INSERT trigger to use audit_tail + FOR UPDATE
-- ------------------------------------------------------------
-- The trigger also ASSIGNS id inside the row-lock. Without this,
-- nextval() fires before the trigger (via the BIGSERIAL default), so
-- two concurrent writers can obtain ids 101 and 102 in nextval order
-- but acquire the tail lock in the reverse order. The chain then
-- references rows out-of-sequence and pb_verify_audit_chain — which
-- walks by id ASC — reports `valid=false` even though every individual
-- hash is correct. By deriving id from `last_entry_id + 1` inside the
-- lock, id ordering matches chain ordering by construction.
--
-- Callers MUST NOT supply an explicit id when inserting into
-- agent_access_log — the trigger overwrites any value.
CREATE OR REPLACE FUNCTION pb_audit_hashchain_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_prev BYTEA;
    v_id   BIGINT;
BEGIN
    -- Row-level lock on the single-row tail table. Unlike advisory
    -- locks, SELECT ... FOR UPDATE re-reads the LATEST committed
    -- value after the lock is granted, so a waiter that started
    -- with a stale statement snapshot still observes the previous
    -- writer's committed update.
    SELECT last_entry_hash, last_entry_id + 1
        INTO v_prev, v_id
        FROM audit_tail
        WHERE id = 1
        FOR UPDATE;

    IF v_prev IS NULL THEN
        -- audit_tail missing — migration not applied cleanly.
        RAISE EXCEPTION 'audit_tail row missing — init-db/022 not applied';
    END IF;

    -- Override the BIGSERIAL-assigned id so it matches chain order.
    NEW.id := v_id;

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

    -- Advance the tail pointer in the same transaction. The row
    -- lock held above prevents concurrent writers from reading
    -- this new value until we commit, and guarantees they read it
    -- afterwards.
    UPDATE audit_tail
        SET last_entry_hash = NEW.entry_hash,
            last_entry_id   = NEW.id,
            updated_at      = now()
        WHERE id = 1;

    -- The BIGSERIAL sequence has already advanced (via the column
    -- DEFAULT) past v_id, so it stays ahead of trigger-assigned ids.
    -- The "wasted" sequence values are harmless — BIGINT has room.

    RETURN NEW;
END;
$$;

-- Trigger definition itself is unchanged (BEFORE INSERT FOR EACH ROW).
-- No DROP/RECREATE needed since CREATE OR REPLACE FUNCTION swaps the body.

-- ------------------------------------------------------------
-- Rewrite checkpoint-and-prune to drop the advisory lock
-- ------------------------------------------------------------
-- Takes the same row lock as the trigger, so pruning can't race
-- with concurrent audit writers. The tail hash itself is not
-- modified by pruning: audit_archive.last_verified_hash preserves
-- the hash at the prune boundary, and audit_tail continues to
-- point at the newest live row.
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
    v_ignored      BYTEA;
BEGIN
    IF p_retention_days IS NULL OR p_retention_days < 1 THEN
        RAISE EXCEPTION 'retention_days must be >= 1, got %', p_retention_days;
    END IF;

    v_cutoff := now() - (p_retention_days || ' days')::INTERVAL;

    -- Serialize against the trigger using the same tail lock.
    -- We discard the returned value; we only need the lock.
    SELECT last_entry_hash INTO v_ignored
        FROM audit_tail
        WHERE id = 1
        FOR UPDATE;

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
