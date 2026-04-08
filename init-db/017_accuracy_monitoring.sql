-- ============================================================
-- 017_accuracy_monitoring.sql — Accuracy Monitoring (EU AI Act Art. 15)
-- ============================================================
-- Two pieces:
--   v_feedback_windowed — windowed aggregates over search_feedback
--                         (1h, 24h, 7d) consumed by pb-worker every
--                         5 minutes to push Prometheus gauges.
--
--   embedding_reference_set — deployment-snapshot baseline of vector
--                             centroids per Qdrant collection. The
--                             worker compares fresh document
--                             centroids against this baseline to
--                             detect embedding drift.
-- ============================================================

-- ------------------------------------------------------------
-- Windowed feedback metrics
-- ------------------------------------------------------------
-- One row per (window_label, collection). Empty result rate is
-- computed from rows where result_ids is empty (or rating == 1 with
-- the comment "no results"). The simpler form below treats an empty
-- result_ids array as the empty signal.
CREATE OR REPLACE VIEW v_feedback_windowed AS
WITH windows(window_label, since) AS (
    VALUES
        ('1h',  now() - INTERVAL '1 hour'),
        ('24h', now() - INTERVAL '24 hours'),
        ('7d',  now() - INTERVAL '7 days')
)
SELECT
    w.window_label,
    COALESCE(f.collection, '_all_')                                AS collection,
    COUNT(f.id)::BIGINT                                             AS sample_count,
    -- For empty windows the metrics are NULL (not 0.0) so dashboards
    -- and Prometheus alerts can distinguish "no data" from "all zeros"
    -- and the worker can skip exporting bogus 0.0 gauge values.
    AVG(f.rating)::FLOAT                                            AS avg_rating,
    AVG(
        CASE WHEN f.id IS NULL THEN NULL
             WHEN COALESCE(array_length(f.result_ids, 1), 0) = 0
                  THEN 1.0
             ELSE 0.0 END
    )::FLOAT                                                        AS empty_result_rate,
    AVG(
        CASE WHEN f.id IS NULL OR f.rerank_scores IS NULL THEN NULL
             ELSE (
                 SELECT AVG((value::TEXT)::FLOAT)
                 FROM jsonb_each(f.rerank_scores)
             )
        END
    )::FLOAT                                                        AS avg_rerank_score
FROM windows w
LEFT JOIN search_feedback f
       ON f.created_at >= w.since
GROUP BY w.window_label, f.collection
ORDER BY w.window_label, f.collection NULLS FIRST;

COMMENT ON VIEW v_feedback_windowed IS
    'Windowed search-quality metrics (EU AI Act Art. 15). Read by pb-worker every 5 min.';

-- ------------------------------------------------------------
-- Embedding reference set (deployment baseline for drift detection)
-- ------------------------------------------------------------
-- Each row is one sampled centroid from a collection. The worker
-- seeds this table on first run (per collection) and compares fresh
-- document centroids against the stored vectors via cosine distance.
CREATE TABLE IF NOT EXISTS embedding_reference_set (
    id            BIGSERIAL PRIMARY KEY,
    collection    VARCHAR(100) NOT NULL,
    seeded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    sample_count  INTEGER NOT NULL,
    embedding_dim INTEGER NOT NULL,
    centroid      DOUBLE PRECISION[] NOT NULL,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_embedding_reference_collection
    ON embedding_reference_set(collection, seeded_at DESC);

COMMENT ON TABLE embedding_reference_set IS
    'Deployment-snapshot embedding centroids per collection. Used by '
    'pb-worker drift detection (EU AI Act Art. 15).';
COMMENT ON COLUMN embedding_reference_set.centroid IS
    'Mean vector across sample_count documents at seed time.';
