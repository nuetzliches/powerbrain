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

Two options depending on your compliance posture:

**Option A — Archive the broken segment and start fresh.**
Use the retention prune with the smallest retention permitted
(`retention_days=1`) during a maintenance window. This writes an
`audit_archive` row covering every entry older than 24 hours —
including the broken segment — marked with the verifier's result
(`chain_valid=false`), then deletes those rows. Anything newer than
24 hours is preserved.

```sql
-- Maintenance window. Blocks audit writes briefly while the
-- checkpoint runs; run during a low-traffic period.
SELECT * FROM pb_audit_checkpoint_and_prune(1);
-- audit_archive now has a row with chain_valid=false recording the
-- extent of the fork for future forensic review; the live log only
-- contains the past 24 hours.
```

If the entire log is within the last 24 hours and you still need a
clean slate, stop all ingest traffic, then run a manual truncate in a
transaction:

```sql
BEGIN;
-- Archive the current tail for forensic reference
INSERT INTO audit_archive (archived_at, last_entry_id, last_verified_hash,
                           row_count, chain_valid, first_invalid_id,
                           retention_cutoff)
SELECT now(), last_entry_id, last_entry_hash,
       (SELECT count(*) FROM agent_access_log), false, NULL, now()
  FROM audit_tail WHERE id = 1;
TRUNCATE agent_access_log RESTART IDENTITY;
UPDATE audit_tail SET last_entry_hash = last_verified_hash,
                      last_entry_id   = 0,
                      updated_at      = now()
  WHERE id = 1;
COMMIT;
```

After either approach, `pb_verify_audit_chain()` verifies from the
first surviving row (or `audit_archive.last_verified_hash` if the
table is empty) and returns `valid=true`.

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
