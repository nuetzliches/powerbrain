-- ============================================================
--  Baustein 3: Evaluation + Feedback-Loop
--  Tabellen für Suchergebnis-Feedback und Offline-Evaluation
-- ============================================================

-- Agenten-Feedback zu Suchergebnissen
CREATE TABLE search_feedback (
    id              BIGSERIAL PRIMARY KEY,
    query           TEXT NOT NULL,
    result_ids      TEXT[] NOT NULL,
    rating          INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    agent_id        VARCHAR(100) NOT NULL,
    comment         TEXT,
    relevant_ids    TEXT[],          -- Vom Agenten als hilfreich markiert
    irrelevant_ids  TEXT[],          -- Vom Agenten als irrelevant markiert
    collection      VARCHAR(100),
    rerank_scores   JSONB,           -- Rerank-Scores zum Zeitpunkt des Feedbacks
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_feedback_query   ON search_feedback(query);
CREATE INDEX idx_feedback_rating  ON search_feedback(rating);
CREATE INDEX idx_feedback_agent   ON search_feedback(agent_id);
CREATE INDEX idx_feedback_time    ON search_feedback(created_at);

-- Testset für Offline-Evaluation
CREATE TABLE eval_test_set (
    id                SERIAL PRIMARY KEY,
    query             TEXT NOT NULL,
    expected_ids      TEXT[],           -- Bekannt relevante Dokument-IDs
    expected_keywords TEXT[],           -- Begriffe die im Ergebnis vorkommen sollten
    collection        VARCHAR(100) DEFAULT 'pb_general',
    category          VARCHAR(50),      -- z.B. "pricing", "technical", "compliance"
    created_at        TIMESTAMPTZ DEFAULT now()
);

-- Evaluation-Läufe (gespeicherte Ergebnisse von run_eval.py)
CREATE TABLE eval_runs (
    id              SERIAL PRIMARY KEY,
    run_date        TIMESTAMPTZ DEFAULT now(),
    test_count      INTEGER,
    avg_precision   FLOAT,
    avg_recall      FLOAT,
    avg_mrr         FLOAT,            -- Mean Reciprocal Rank
    avg_latency_ms  FLOAT,
    details         JSONB,            -- Per-Query Ergebnisse
    config          JSONB             -- Modell, Reranker, etc.
);
