-- ============================================================
-- 025_audit_force_reset.sql — Operator helper for audit-chain
-- resets in test/staging deployments (#97).
-- ============================================================
-- Replaces the multi-statement manual procedure documented in
-- docs/audit-chain-migration.md (Continuity / Genesis reset) with a
-- single function call. The manual SQL was footgun-prone — see the
-- docs corrections in #93 — and easy to get wrong on a stressed
-- staging environment.
--
-- WARNING: NOT for production. There is no production-environment
-- guard yet (see #97 follow-up). The safety net is:
--   1. SECURITY DEFINER + REVOKE EXECUTE FROM PUBLIC → only the DB
--      owner / superuser can call it.
--   2. Row lock on audit_tail → cannot race with concurrent inserts.
--   3. Both modes archive the current tail to audit_archive with
--      chain_valid=false for forensic continuity.
--   4. The migration 023 verifier (#94) detects an inconsistent seed
--      after a misuse, so a forgotten archive truncate is surfaced
--      proactively instead of breaking on the next insert.
-- ============================================================

CREATE OR REPLACE FUNCTION pb_audit_force_reset(
    p_mode TEXT DEFAULT 'continuity'  -- 'continuity' | 'genesis'
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
    v_genesis   BYTEA := '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA;
BEGIN
    IF p_mode NOT IN ('continuity', 'genesis') THEN
        RAISE EXCEPTION 'p_mode must be ''continuity'' or ''genesis'', got %', p_mode;
    END IF;

    -- Same row lock as the trigger; cannot race with concurrent inserts.
    SELECT last_entry_hash, last_entry_id INTO v_tail_hash, v_tail_id
        FROM audit_tail WHERE id = 1 FOR UPDATE;

    IF v_tail_hash IS NULL THEN
        RAISE EXCEPTION 'audit_tail row missing — init-db/022 not applied';
    END IF;

    SELECT COUNT(*) INTO v_count FROM agent_access_log;

    -- 1. Archive the current tail with chain_valid=false marker.
    --    Records that a forced reset happened, regardless of mode.
    INSERT INTO audit_archive(
        archived_at, last_entry_id, last_verified_hash,
        row_count, chain_valid, first_invalid_id, retention_cutoff
    ) VALUES (
        now(), v_tail_id, v_tail_hash,
        v_count, FALSE, NULL, now()
    );

    -- 2. Truncate the live log (every mode).
    --    RESTART IDENTITY also resets the BIGSERIAL sequence so the
    --    next id starts from 1 again.
    TRUNCATE agent_access_log RESTART IDENTITY;

    IF p_mode = 'continuity' THEN
        -- New chain continues from the archived hash.
        -- pb_verify_audit_chain() walks straight through (the
        -- archive hash matches audit_tail.last_entry_hash, the seed
        -- check from migration 023 passes).
        UPDATE audit_tail
            SET last_entry_hash = v_tail_hash,
                last_entry_id   = 0,
                updated_at      = now()
            WHERE id = 1;
        RETURN QUERY SELECT v_count, v_tail_hash, v_tail_hash;
    ELSE
        -- genesis: also clear the archive and reset tail to zero hash.
        -- All forensic context is discarded; the chain restarts as if
        -- the database were freshly initialised.
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

COMMENT ON FUNCTION pb_audit_force_reset(TEXT) IS
    'TEST/STAGING ONLY. Atomically resets the audit chain. p_mode=continuity preserves audit_archive (new chain cross-links to archived hash via the migration 023 seed check). p_mode=genesis additionally truncates audit_archive and reseeds audit_tail to 32 zero bytes. No production-environment guard yet — see #97 follow-up. EXECUTE granted to DB owner / superuser only.';

REVOKE EXECUTE ON FUNCTION pb_audit_force_reset(TEXT) FROM PUBLIC;
