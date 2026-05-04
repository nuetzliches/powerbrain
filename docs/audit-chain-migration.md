# Audit hash-chain migration (022_audit_tail_pointer)

**TL;DR** — Applying this migration fixes concurrent-writer chain forks
going forward. It does **not** heal broken chains in already-running
deployments: by design, EU AI Act Art. 12 requires tamper-evidence, not
tamper-healing. Operators with `audit_integrity.valid = false` should
archive the broken segment and start a fresh chain, following the steps
below.

## Why this exists

Before `init-db/022_audit_tail_pointer.sql`, the BEFORE INSERT trigger
on `agent_access_log` serialized writes via `pg_advisory_xact_lock`,
then read the tail hash with `SELECT ... ORDER BY id DESC LIMIT 1`.
Under concurrent ingests (issue
[#59](https://github.com/nuetzliches/powerbrain/issues/59)), the
advisory lock didn't protect against stale statement snapshots: two
writers observed the same `prev_hash` and both inserted with it. The
chain forked, `pb_verify_audit_chain()` returned `valid=false`, and
`get_system_info` began reporting
`"audit_integrity": { "valid": false }`.

The migration replaces the advisory lock with a single-row tail pointer
table (`audit_tail`). The trigger reads the tail via
`SELECT ... FOR UPDATE`, which — unlike advisory locks — forces Postgres
to re-read the latest committed value after the lock is granted. The
second writer correctly observes the first writer's committed hash and
chains to it.

## What changes for an operator

| Aspect | Before (014) | After (022) |
|---|---|---|
| Lock primitive | `pg_advisory_xact_lock(847291)` | `SELECT ... FOR UPDATE` on `audit_tail` row 1 |
| Read source | `agent_access_log ORDER BY id DESC LIMIT 1` | `audit_tail.last_entry_hash` |
| Safe under concurrency | No — see issue #59 | Yes |
| Verifier function | `pb_verify_audit_chain()` unchanged | `pb_verify_audit_chain()` unchanged |
| New table | — | `audit_tail` (1 row, RLS on) |

`pb_verify_audit_chain()` and `pb_verify_audit_chain_tail()` are
**unchanged** — they walk `agent_access_log` row by row, independent of
the tail pointer. Existing archives in `audit_archive` remain valid.

## If your deployment already has a broken chain

Applying the migration seeds `audit_tail.last_entry_hash` from the
current newest row's `entry_hash`. From that row onward, new writes
chain correctly. The broken segment **stays broken** and
`pb_verify_audit_chain()` will still return `valid=false` because it
walks the whole history.

### Recovery via `pb_audit_force_reset()` (migration 025+)

> **TEST / STAGING ONLY.** The function is `SECURITY DEFINER` with
> `EXECUTE` revoked from `PUBLIC`, so only the DB owner / superuser
> can call it. There is no production-environment guard yet — see
> [#97](https://github.com/nuetzliches/powerbrain/issues/97). Treat
> the manual paths below as the production-safe alternative.

A single function call handles both reset modes atomically (row lock
on `audit_tail`, cannot race with concurrent inserts). Both modes
write an `audit_archive` entry with `chain_valid=false` recording the
forced reset; **only `continuity` preserves it** afterwards. `genesis`
deletes every `audit_archive` row including that just-written marker,
so the forensic trail of "a forced reset happened" survives only in
`continuity` mode.

```sql
-- Continuity (preserve archive, cross-link new chain)
SELECT * FROM pb_audit_force_reset('continuity');

-- Full genesis (also wipes archive)
SELECT * FROM pb_audit_force_reset('genesis');

-- With operator-supplied purpose for forensic provenance (#101, migration 026+)
SELECT * FROM pb_audit_force_reset('continuity', 'CI fixture cleanup');
```

Each call returns `archived_rows`, `archived_hash`, `new_tail_hash`
so the operator can confirm what was archived and what the next
chain anchors to. The default mode is `continuity` — calling
`pb_audit_force_reset()` without arguments preserves the archive.

From migration 026 onward, the function also writes a self-record
into `agent_access_log` **before** the truncate, so the forced reset
is captured in the cryptographic chain that gets archived (continuity
mode) or appears briefly in Postgres statement logs (genesis mode).
`audit_archive.reset_caller` and `audit_archive.reset_purpose`
columns capture the operator context. As a result, `archived_rows`
includes the self-record (typically `+1` over the previous behavior),
and `archived_hash` points to the self-record's `entry_hash` — the
correct cryptographic snapshot of the archived chain.

If your deployment is on migration 024 or earlier, fall back to the
manual procedures below.

### Manual recovery (any version)

Two options depending on your compliance posture:

**Option A — Archive the broken segment and start fresh.**
Use the retention prune with the smallest retention permitted
(`retention_days=1`) during a maintenance window. This writes an
`audit_archive` row covering every entry older than 24 hours —
including the broken segment — marked with the verifier's result
(`chain_valid=false`).

```sql
-- Maintenance window. Blocks audit writes briefly while the
-- checkpoint runs; run during a low-traffic period.
SELECT * FROM pb_audit_checkpoint_and_prune(1);
-- audit_archive now has a row with chain_valid=false recording the
-- extent of the fork for future forensic review.
```

> **Caveat — broken chains are not pruned.** `pb_audit_checkpoint_and_prune`
> only deletes rows after a successful end-to-end verify
> (`IF v_verify.valid THEN DELETE`, fail-closed). On a broken chain the
> archive entry is recorded but the rows stay in `agent_access_log`, and
> `pb_verify_audit_chain()` continues to report `valid=false`. Fall through
> to the manual TRUNCATE below.

For a complete clean slate (or whenever the prune above could not
delete because the chain is broken), stop all ingest traffic, then
run a manual truncate in a transaction:

```sql
BEGIN;
-- Archive the current tail for forensic reference. The CTE feeds the
-- archive's last_verified_hash back into audit_tail so the next chain
-- continues from the same checkpoint hash without a genesis reseed.
WITH archived AS (
    INSERT INTO audit_archive (archived_at, last_entry_id, last_verified_hash,
                                row_count, chain_valid, first_invalid_id,
                                retention_cutoff)
    SELECT now(), last_entry_id, last_entry_hash,
           (SELECT count(*) FROM agent_access_log), false, NULL, now()
      FROM audit_tail WHERE id = 1
    RETURNING last_verified_hash
)
UPDATE audit_tail
   SET last_entry_hash = (SELECT last_verified_hash FROM archived),
       last_entry_id   = 0,
       updated_at      = now()
 WHERE id = 1;
TRUNCATE agent_access_log RESTART IDENTITY;
COMMIT;
```

For a full genesis reset (test/staging only), the `audit_tail` AND
`audit_archive` must both be reset, otherwise `pb_verify_audit_chain()`
seeds `expected_prev` from the most recent archive row's
`last_verified_hash` and the next inserted row — which carries the
Genesis `prev_hash` from the trigger — fails verification at id=1:

```sql
BEGIN;
TRUNCATE agent_access_log RESTART IDENTITY;
DELETE FROM audit_archive;     -- discard the forensic trail too
UPDATE audit_tail
   SET last_entry_hash = '\x0000000000000000000000000000000000000000000000000000000000000000'::BYTEA,
       last_entry_id   = 0,
       updated_at      = now()
 WHERE id = 1;
COMMIT;
```

If preserving the forensic archive matters (regulator-facing or
post-mortem reviews), use the Continuity-reset above instead — the
new chain cross-links to the archive's `last_verified_hash`, so
`pb_verify_audit_chain()` walks straight through.

After either approach, `pb_verify_audit_chain()` verifies from the
first surviving row (or `audit_archive.last_verified_hash` if the
table is empty) and returns `valid=true`.

> **From migration 023 onward**, the verifier additionally cross-checks
> `audit_tail.last_entry_hash` against the resolved seed when the log
> is empty. If they disagree (e.g. a genesis reset that forgot to
> truncate `audit_archive`), it now returns
> `valid=false, first_invalid_id=1` instead of silently passing —
> see [#94](https://github.com/nuetzliches/powerbrain/issues/94).

**Option B — Keep the broken rows for forensics, accept valid=false.**
Do nothing. `get_system_info` keeps reporting `valid=false`. If you
later run `pb_verify_audit_chain(from_id, to_id)` with bounds that
skip the forked segment, you get per-range validation.

Option A is recommended for most deployments — the forked segment
can't be trusted for regulator-facing attestation anyway.

## Verification

After applying the migration:

```sql
-- 1. audit_tail row exists and matches the newest log entry
SELECT t.last_entry_id, a.id AS newest_log_id,
       t.last_entry_hash = a.entry_hash AS hashes_match
FROM audit_tail t
LEFT JOIN agent_access_log a ON a.id = (SELECT MAX(id) FROM agent_access_log);

-- 2. Chain verifies (post-reset if you chose Option A)
SELECT * FROM pb_verify_audit_chain();
```

The integration test
`tests/integration/test_audit_chain_concurrency.py` spawns 16 writers
each inserting 100 rows; after, the chain must verify. Enable it with
`RUN_INTEGRATION_TESTS=1`.

## Rollback

If you ever need to revert to the 014 behaviour, swap the trigger body
back to the advisory-lock version. The `audit_tail` row can stay — the
new trigger won't reference it. **Do not drop the table** without first
verifying no trigger or procedure references it.
