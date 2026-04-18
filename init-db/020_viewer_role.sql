-- 020_viewer_role.sql: Widen api_keys.agent_role CHECK constraint to allow 'viewer'.
--
-- The 'viewer' role is part of the OPA access matrix since its inception
-- (opa-policies/pb/data.json → roles: ["viewer", "analyst", "developer", "admin"])
-- but the DB CHECK originally only accepted analyst/developer/admin. The sales-demo
-- profile ships a viewer-scoped demo key, so the CHECK must accept the role.
--
-- Safe on fresh installs: 010_api_keys.sql already declares the widened CHECK,
-- so the DROP below will find an equivalent constraint and ALTER TABLE ADD
-- re-adds the identical definition — idempotent.

DO $$
DECLARE
    cons_name TEXT;
BEGIN
    SELECT conname INTO cons_name
      FROM pg_constraint
     WHERE conrelid = 'api_keys'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) ILIKE '%agent_role%'
     LIMIT 1;

    IF cons_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE api_keys DROP CONSTRAINT %I', cons_name);
    END IF;

    ALTER TABLE api_keys
        ADD CONSTRAINT api_keys_agent_role_check
        CHECK (agent_role IN ('viewer', 'analyst', 'developer', 'admin'));
END
$$;
