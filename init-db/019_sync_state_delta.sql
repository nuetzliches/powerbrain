-- 019_sync_state_delta.sql — Add delta_link support for Office 365 sync
--
-- Microsoft Graph uses opaque delta tokens instead of commit SHAs.
-- The delta_links JSONB column stores per-resource delta links:
--   {"drive:abc123": "https://graph.../delta?token=...",
--    "mail:user@corp.com:folder123": "https://graph.../delta?token=...",
--    "teams:team123:channel456": "https://graph.../delta?token=..."}

ALTER TABLE repo_sync_state
    ADD COLUMN IF NOT EXISTS delta_links JSONB DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) DEFAULT 'git';

COMMENT ON COLUMN repo_sync_state.delta_links IS
    'Per-resource delta tokens for Microsoft Graph incremental sync';
COMMENT ON COLUMN repo_sync_state.source_type IS
    'Adapter type: git, office365';
