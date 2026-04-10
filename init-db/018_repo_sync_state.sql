-- 018_repo_sync_state.sql — Track GitHub/Git repository sync state
--
-- Each configured repository gets a row tracking its last synced commit.
-- Used by the sync service for incremental updates.

CREATE TABLE IF NOT EXISTS repo_sync_state (
    repo_name       VARCHAR(200) PRIMARY KEY,
    repo_url        VARCHAR(500) NOT NULL,
    branch          VARCHAR(200) NOT NULL DEFAULT 'main',
    last_commit_sha VARCHAR(40),
    last_synced_at  TIMESTAMPTZ,
    file_count      INTEGER DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'pending'
                    CHECK (status IN ('pending', 'syncing', 'ok', 'error')),
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_repo_sync_status ON repo_sync_state(status);
