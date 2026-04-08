-- ============================================================
--  Building block 3: evaluation + feedback loop
--  Tables for search-result feedback and offline evaluation
-- ============================================================

-- Agent feedback on search results
CREATE TABLE search_feedback (
    id              BIGSERIAL PRIMARY KEY,
    query           TEXT NOT NULL,
    result_ids      TEXT[] NOT NULL,
    rating          INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    agent_id        VARCHAR(100) NOT NULL,
    comment         TEXT,
    relevant_ids    TEXT[],          -- Marked as helpful by the agent
    irrelevant_ids  TEXT[],          -- Marked as irrelevant by the agent
    collection      VARCHAR(100),
    rerank_scores   JSONB,           -- Rerank scores at the time of the feedback
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_feedback_query   ON search_feedback(query);
CREATE INDEX idx_feedback_rating  ON search_feedback(rating);
CREATE INDEX idx_feedback_agent   ON search_feedback(agent_id);
CREATE INDEX idx_feedback_time    ON search_feedback(created_at);

-- Test set for offline evaluation
CREATE TABLE eval_test_set (
    id                SERIAL PRIMARY KEY,
    query             TEXT NOT NULL,
    expected_ids      TEXT[],           -- Known relevant document IDs
    expected_keywords TEXT[],           -- Terms that should appear in the result
    collection        VARCHAR(100) DEFAULT 'pb_general',
    category          VARCHAR(50),      -- e.g. "pricing", "technical", "compliance"
    created_at        TIMESTAMPTZ DEFAULT now()
);

-- Evaluation runs (stored results from run_eval.py)
CREATE TABLE eval_runs (
    id              SERIAL PRIMARY KEY,
    run_date        TIMESTAMPTZ DEFAULT now(),
    test_count      INTEGER,
    avg_precision   FLOAT,
    avg_recall      FLOAT,
    avg_mrr         FLOAT,            -- Mean Reciprocal Rank
    avg_latency_ms  FLOAT,
    details         JSONB,            -- Per-query results
    config          JSONB             -- Model, reranker, etc.
);
