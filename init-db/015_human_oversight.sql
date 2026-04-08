-- ============================================================
-- 015_human_oversight.sql — Human Oversight Controls (EU AI Act Art. 14)
-- ============================================================
-- Two tables:
--   pb_circuit_breaker_state — single row "active/inactive" kill-switch
--     for all data-retrieval tools (search_knowledge, query_data,
--     get_code_context, get_document). Checked in _dispatch() with a
--     5-second in-process cache.
--
--   pending_reviews — queued requests that need human approval before
--     they run. Search-path creates rows, admin approves/denies via the
--     review_pending MCP tool, agents poll via get_review_status.
--     Timeouts are handled by the pb-worker pending_review_timeout job.
-- ============================================================

-- ------------------------------------------------------------
-- Circuit breaker: single-row table by convention (id = 1)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pb_circuit_breaker_state (
    id         SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    active     BOOLEAN NOT NULL DEFAULT FALSE,
    reason     TEXT,
    set_by     VARCHAR(100),
    set_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the singleton row
INSERT INTO pb_circuit_breaker_state (id, active, reason)
    VALUES (1, FALSE, NULL)
    ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE pb_circuit_breaker_state IS
    'Single-row kill-switch for data-retrieval tools (EU AI Act Art. 14). '
    'When active=true, _dispatch() short-circuits all data tools.';

-- ------------------------------------------------------------
-- pending_reviews: async approval queue
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_reviews (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        VARCHAR(100) NOT NULL,
    agent_role      VARCHAR(50)  NOT NULL,
    tool            VARCHAR(100) NOT NULL,
    arguments       JSONB        NOT NULL,
    classification  VARCHAR(50),
    status          VARCHAR(20)  NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    reason          TEXT,
    decision_by     VARCHAR(100),
    decision_at     TIMESTAMPTZ,
    result_payload  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_reviews_status
    ON pending_reviews(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_reviews_agent
    ON pending_reviews(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_reviews_expires
    ON pending_reviews(expires_at)
    WHERE status = 'pending';

COMMENT ON TABLE pending_reviews IS
    'Async approval queue for the Human Oversight flow (EU AI Act Art. 14). '
    'Agents receive {status: pending, review_id} and poll via get_review_status.';
