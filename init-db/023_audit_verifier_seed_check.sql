-- ============================================================
-- 023_audit_verifier_seed_check.sql — Detect inconsistent seeds
-- on an empty agent_access_log (#94).
-- ============================================================
-- Background:
--   pb_verify_audit_chain() seeds v_expected_prev from
--   audit_archive.last_verified_hash and walks agent_access_log
--   row-by-row. When the log is empty the FOR loop body never runs
--   and the function unconditionally returns valid=true. That's
--   wrong if audit_tail.last_entry_hash (the seed for the *next*
--   insert via the trigger) disagrees with the archive seed used
--   by the verifier — the next insert is guaranteed to fail
--   verification at id=1 because Genesis != archive checkpoint.
--
--   Operators relying on pb_verify_audit_chain() as a pre-flight
--   health check after a genesis-style reset got a green light
--   immediately followed by valid=false on the first write.
--
-- Fix:
--   In the empty-log path (v_count = 0 AND p_start_id resolves to
--   the chain head), look up audit_tail.last_entry_hash and compare
--   it to the resolved seed. If they differ, return
--   valid=false, first_invalid_id=1 with a synthetic last_valid_hash
--   carrying the (incorrect) archive seed for diagnostics.
--
-- Compatibility:
--   * Function signature is unchanged — CREATE OR REPLACE FUNCTION.
--   * Range-scoped calls (p_start_id > 1) keep their existing
--     behaviour: the caller is asking about a specific window, not
--     the chain head, so a tail comparison would be misleading.
--   * audit_tail exists since migration 022 — the lookup is safe.
-- ============================================================

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
    v_tail_hash     BYTEA;
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

    -- ── Seed-mismatch detection on an empty range (#94) ──
    -- The walk above terminated without examining any rows. If the
    -- caller is asking about the chain head (p_start_id NULL or <=1),
    -- the next insert via the trigger will use audit_tail.last_entry_hash
    -- as prev_hash. If that disagrees with the archive seed we just
    -- resolved, the chain will break on the very first write. Surface
    -- it now instead of silently passing.
    IF v_count = 0 AND (p_start_id IS NULL OR p_start_id <= 1) THEN
        SELECT last_entry_hash INTO v_tail_hash
            FROM audit_tail WHERE id = 1;
        IF v_tail_hash IS NOT NULL
           AND v_tail_hash IS DISTINCT FROM v_expected_prev THEN
            RETURN QUERY SELECT FALSE, 1::BIGINT, 0::BIGINT, v_expected_prev;
            RETURN;
        END IF;
    END IF;

    RETURN QUERY SELECT TRUE, NULL::BIGINT, v_count, v_last_valid;
END;
$$;

COMMENT ON FUNCTION pb_verify_audit_chain(BIGINT, BIGINT) IS
'Verifies the agent_access_log hash chain. Resolves the starting prev_hash from the previous row (or audit_archive.last_verified_hash, or genesis). On an empty range with chain-head bounds, additionally checks that audit_tail.last_entry_hash matches the resolved seed — guards against post-reset states where the next insert would fail at id=1 (issue #94).';
