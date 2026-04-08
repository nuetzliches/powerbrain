-- ============================================================
-- 016_data_quality.sql — Data Quality Tracking (EU AI Act Art. 10)
-- ============================================================
-- Adds quality scoring columns to documents_meta and a lightweight
-- log of rejected documents so Deployers can audit the quality gate.
-- Actual score computation lives in ingestion/quality.py; the gate
-- decision is made by OPA policy pb.ingestion.quality_gate.
-- ============================================================

ALTER TABLE documents_meta
    ADD COLUMN IF NOT EXISTS quality_score   REAL,
    ADD COLUMN IF NOT EXISTS quality_details JSONB;

CREATE INDEX IF NOT EXISTS idx_documents_meta_quality_score
    ON documents_meta(quality_score)
    WHERE quality_score IS NOT NULL;

-- Rejection log: documents that failed the quality gate are not
-- stored in documents_meta (they never enter retrieval), but a
-- minimal audit row is written here for Art. 10 traceability.
CREATE TABLE IF NOT EXISTS ingestion_rejections (
    id                BIGSERIAL PRIMARY KEY,
    rejected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_type       VARCHAR(50),
    project           VARCHAR(100),
    classification    VARCHAR(50),
    quality_score     REAL,
    min_required      REAL,
    reason            TEXT,
    quality_details   JSONB,
    sample_snippet    TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingestion_rejections_rejected_at
    ON ingestion_rejections(rejected_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_rejections_source_type
    ON ingestion_rejections(source_type);

COMMENT ON COLUMN documents_meta.quality_score IS
    'Composite quality score 0.0-1.0 (EU AI Act Art. 10 data quality). '
    'Computed by ingestion/quality.py, gated by OPA pb.ingestion.quality_gate.';

COMMENT ON COLUMN documents_meta.quality_details IS
    'Per-factor quality breakdown (length, language, pii_density, encoding, metadata_completeness).';

COMMENT ON TABLE ingestion_rejections IS
    'Audit log of documents rejected by the quality gate (Art. 10 traceability).';
