-- ============================================================
-- 026_audit_force_reset_provenance.sql — Forensic provenance for
-- pb_audit_force_reset() (#101).
-- ============================================================
-- Migration 025 introduced pb_audit_force_reset() but left no
-- in-chain record of who called it. The audit_archive row marker
-- captured chain_valid=false and the previous tail hash, but neither
-- the caller's identity nor the operator's purpose. For a function
-- deliberately named force_reset, that gap is unhelpful: when CI
-- flakes find a chain break, the first question is "did the test
-- fixture reset the chain — and why?".
--
-- This migration applies BOTH mitigations from the issue's option
-- list (#101 "belt and suspenders"):
--
--   1. Extend `audit_archive` with `reset_caller` (defaults to
--      current_user, populated by the function) and `reset_purpose`
--      (operator-supplied free-text). Survives in continuity mode;
--      lost in genesis (audit_archive is truncated by design).
--
--   2. Write a self-record into `agent_access_log` BEFORE the
--      TRUNCATE so the reset action is captured in the cryptographic
--      chain that is about to be archived. The hash chain trigger
--      links it to the previous tail, so a forensic audit can detect
--      tampering on the historical reset record itself.
--      In continuity mode the self-record is part of audit_archive's
--      row_count + last_verified_hash (the chain ends at the
--      self-record). In genesis mode the archive is then deleted, but
--      Postgres statement logs still record the function call.
--
-- The function signature gains an optional `p_purpose TEXT` parameter
-- that defaults to NULL. Existing callers (`pb_audit_force_reset()`,
-- `pb_audit_force_reset('continuity')`, `pb_audit_force_reset('genesis')`)
-- keep working unchanged.
--
-- Behavior change visible to callers: `archived_rows` now includes the
-- self-record (typically +1 over the previous behavior), and
-- `archived_hash` / audit_archive.last_verified_hash now point to the
-- self-record's entry_hash instead of the pre-call tail. This is the
-- correct cryptographic value for the archive snapshot — anyone
-- replaying pb_verify_audit_chain over the archive will arrive at the
-- self-record's hash. Live PG tests in
-- mcp-server/tests/test_audit_integrity.py have been updated to match.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Extend audit_archive with provenance columns
-- ------------------------------------------------------------
ALTER TABLE audit_archive
    ADD COLUMN IF NOT EXISTS reset_caller  TEXT,
    ADD COLUMN IF NOT EXISTS reset_purpose TEXT;

COMMENT ON COLUMN audit_archive.reset_caller  IS
    'DB role that called pb_audit_force_reset(). NULL for archive rows produced by retention pruning.';
COMMENT ON COLUMN audit_archive.reset_purpose IS
    'Operator-supplied purpose passed to pb_audit_force_reset(p_purpose). NULL when no purpose was given.';

-- ------------------------------------------------------------
-- 2. Replace the function with the new two-arg signature
-- ------------------------------------------------------------
-- Drop the migration 025 single-arg version explicitly so we replace
-- it cleanly. The new function has all-default args, so existing
-- zero-arg and one-arg callers keep working without changes.
DROP FUNCTION IF EXISTS pb_audit_force_reset(TEXT);

CREATE OR REPLACE FUNCTION pb_audit_force_reset(
    p_mode    TEXT DEFAULT 'continuity',  -- 'continuity' | 'genesis'
    p_purpose TEXT DEFAULT NULL           -- operator-supplied reason
) RETURNS TABLE(
    archived_rows BIGINT,
    archived_hash BYTEA,
    new_tail_hash BYTEA
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tail_hash BYTEA;
    v_tail_id   BIGINT;
    v_count     BIGINT;
    v_caller    TEXT  := current_user;
    v_genesis   BYTEA := '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA;
BEGIN
    IF p_mode NOT IN ('continuity', 'genesis') THEN
        RAISE EXCEPTION 'p_mode must be ''continuity'' or ''genesis'', got %', p_mode;
    END IF;

    -- 1. Insert self-record BEFORE the lock + truncate so the action
    --    is captured in the cryptographic chain. The hash-chain trigger
    --    will link it to the current tail, take + release the
    --    audit_tail row lock, and advance the tail to the self-record's
    --    entry_hash. The whole call is one transaction, so the
    --    re-acquired lock below sees the self-record's hash.
    INSERT INTO agent_access_log (
        agent_id, agent_role, resource_type, resource_id,
        action, policy_result, policy_reason,
        purpose, request_context
    ) VALUES (
        v_caller,                     -- agent_id: who called force_reset
        'admin',                      -- agent_role
        'audit_chain',                -- resource_type
        'force_reset',                -- resource_id
        'force_reset',                -- action
        'allow',                      -- policy_result
        'pb_audit_force_reset',       -- policy_reason
        p_purpose,                    -- purpose
        jsonb_build_object('mode', p_mode)
    );

    -- 2. Re-acquire the row lock (no-op within the transaction, but
    --    explicit for symmetry with migration 025).
    SELECT last_entry_hash, last_entry_id INTO v_tail_hash, v_tail_id
        FROM audit_tail WHERE id = 1 FOR UPDATE;

    IF v_tail_hash IS NULL THEN
        RAISE EXCEPTION 'audit_tail row missing — init-db/022 not applied';
    END IF;

    SELECT COUNT(*) INTO v_count FROM agent_access_log;

    -- 3. Archive the chain (now ending at the self-record).
    --    chain_valid stays FALSE because this is a forced reset, not
    --    a clean retention checkpoint. reset_caller / reset_purpose
    --    capture the operator context.
    INSERT INTO audit_archive(
        archived_at, last_entry_id, last_verified_hash,
        row_count, chain_valid, first_invalid_id, retention_cutoff,
        reset_caller, reset_purpose
    ) VALUES (
        now(), v_tail_id, v_tail_hash,
        v_count, FALSE, NULL, now(),
        v_caller, p_purpose
    );

    -- 4. Truncate the live log (every mode).
    --    RESTART IDENTITY also resets the BIGSERIAL sequence so the
    --    next id starts from 1 again.
    TRUNCATE agent_access_log RESTART IDENTITY;

    IF p_mode = 'continuity' THEN
        -- New chain continues from the self-record's hash. Anyone
        -- walking pb_verify_audit_chain over the archive arrives at
        -- v_tail_hash; the next live insert chains from v_tail_hash;
        -- the migration 023 seed check passes.
        UPDATE audit_tail
            SET last_entry_hash = v_tail_hash,
                last_entry_id   = 0,
                updated_at      = now()
            WHERE id = 1;
        RETURN QUERY SELECT v_count, v_tail_hash, v_tail_hash;
    ELSE
        -- genesis: also clear the archive and reset tail to zero hash.
        -- The reset_caller / reset_purpose columns are lost here by
        -- design; the only forensic trace is in Postgres statement
        -- logs (which capture every superuser-level function call).
        DELETE FROM audit_archive;
        UPDATE audit_tail
            SET last_entry_hash = v_genesis,
                last_entry_id   = 0,
                updated_at      = now()
            WHERE id = 1;
        RETURN QUERY SELECT v_count, v_tail_hash, v_genesis;
    END IF;
END;
$$;

COMMENT ON FUNCTION pb_audit_force_reset(TEXT, TEXT) IS
    'TEST/STAGING ONLY. Atomically resets the audit chain. p_mode=continuity preserves audit_archive (new chain cross-links to archived hash via the migration 023 seed check). p_mode=genesis additionally truncates audit_archive and reseeds audit_tail to 32 zero bytes. p_purpose is recorded into audit_archive.reset_purpose and the in-chain self-record (#101). No production-environment guard yet — see #97 follow-up. EXECUTE granted to DB owner / superuser only.';

REVOKE EXECUTE ON FUNCTION pb_audit_force_reset(TEXT, TEXT) FROM PUBLIC;
