-- 009_add_source_type.sql
-- Adds source_type column to datasets and documents_meta tables.
-- These columns are defined in 001_schema.sql but may be missing
-- in databases created before the column was added.
-- Idempotent: uses IF NOT EXISTS.

ALTER TABLE datasets
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(50);

COMMENT ON COLUMN datasets.source_type IS 'csv, json, sql_dump, git_repo';

ALTER TABLE documents_meta
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(50);

COMMENT ON COLUMN documents_meta.source_type IS 'csv, json, git, sql_dump';
