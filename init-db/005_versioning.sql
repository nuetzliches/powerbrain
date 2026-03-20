-- ============================================================
--  Baustein 4: Wissens-Versionierung
--  Snapshot-Metadaten + Temporal History (SCD Type 2)
-- ============================================================

-- Snapshot-Metadaten (Qdrant + PG + OPA Policy Commit)
CREATE TABLE knowledge_snapshots (
    id              SERIAL PRIMARY KEY,
    snapshot_name   VARCHAR(255) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    created_by      VARCHAR(100),
    description     TEXT,
    components      JSONB NOT NULL,
    -- Beispiel:
    -- {"qdrant": {"collections": {"knowledge_general": "snap-id-abc"},
    --             "snapshot_urls": [...]},
    --  "postgres": {"tables": ["datasets","dataset_rows"], "row_counts": {...}},
    --  "opa": {"policy_commit": "abc123"}}
    status          VARCHAR(20) DEFAULT 'completed',
    size_bytes      BIGINT
);

CREATE INDEX idx_snapshots_name ON knowledge_snapshots(snapshot_name);
CREATE INDEX idx_snapshots_time ON knowledge_snapshots(created_at);

-- Temporal History für Datasets (SCD Type 2)
CREATE TABLE datasets_history (
    history_id      BIGSERIAL PRIMARY KEY,
    dataset_id      UUID NOT NULL,
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ DEFAULT 'infinity',
    operation       VARCHAR(10) NOT NULL, -- INSERT, UPDATE, DELETE
    data            JSONB NOT NULL,       -- Kompletter Row-Snapshot
    changed_by      VARCHAR(100)
);

CREATE INDEX idx_datasets_hist_id   ON datasets_history(dataset_id);
CREATE INDEX idx_datasets_hist_time ON datasets_history(valid_from, valid_to);

-- Trigger: Automatisch History schreiben bei Änderungen an datasets
CREATE OR REPLACE FUNCTION track_dataset_changes()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
        UPDATE datasets_history SET valid_to = now()
        WHERE dataset_id = OLD.id AND valid_to = 'infinity';
    END IF;
    IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
        INSERT INTO datasets_history (dataset_id, operation, data, changed_by)
        VALUES (
            NEW.id,
            TG_OP,
            row_to_json(NEW)::jsonb,
            current_setting('app.current_user', true)
        );
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_datasets_history
    AFTER INSERT OR UPDATE OR DELETE ON datasets
    FOR EACH ROW EXECUTE FUNCTION track_dataset_changes();
