-- ============================================================
--  Knowledge base – privacy extension
--  Migration: 002_privacy.sql
-- ============================================================

-- Data categories for GDPR purpose binding
CREATE TABLE data_categories (
    id              VARCHAR(50) PRIMARY KEY,
    description     TEXT NOT NULL,
    contains_pii    BOOLEAN DEFAULT false,
    legal_basis     VARCHAR(100),           -- Art. 6 GDPR legal basis
    allowed_purposes TEXT[],                -- Permitted processing purposes
    retention_days  INTEGER NOT NULL DEFAULT 365
);

INSERT INTO data_categories (id, description, contains_pii, legal_basis, allowed_purposes, retention_days) VALUES
('customer_data',   'Customer master data',        true,  'Art. 6 para. 1 lit. b (contract performance)', ARRAY['support','billing','contract_fulfillment'], 730),
('employee_data',   'Employee data',                true,  'Art. 6 para. 1 lit. b (employment contract)', ARRAY['hr_management','payroll'], 1095),
('analytics_data',  'Anonymized analytics data',    false, 'Art. 6 para. 1 lit. f (legitimate interest)', ARRAY['reporting','product_improvement'], 90),
('marketing_data',  'Marketing contacts',           true,  'Art. 6 para. 1 lit. a (consent)',             ARRAY['campaign_management','consent_based_contact'], 365),
('contract_data',   'Contract documents',           true,  'Art. 6 para. 1 lit. b (contract performance)', ARRAY['contract_fulfillment','legal'], 1095),
('accounting_data', 'Accounting data',              true,  'Art. 6 para. 1 lit. c (legal obligation)',    ARRAY['accounting','audit'], 3650),
('technical_data',  'Technical documentation',      false, NULL, ARRAY['development','operations'], 365);

-- Add privacy fields to datasets
ALTER TABLE datasets
    ADD COLUMN data_category     VARCHAR(50) REFERENCES data_categories(id),
    ADD COLUMN contains_pii      BOOLEAN DEFAULT false,
    ADD COLUMN legal_basis        VARCHAR(100),
    ADD COLUMN retention_expires_at TIMESTAMPTZ,
    ADD COLUMN pii_fields         TEXT[],           -- Which fields contain PII
    ADD COLUMN pseudonymized      BOOLEAN DEFAULT false;

CREATE INDEX idx_datasets_retention ON datasets(retention_expires_at)
    WHERE retention_expires_at IS NOT NULL;
CREATE INDEX idx_datasets_pii ON datasets(contains_pii)
    WHERE contains_pii = true;

-- Add privacy fields to documents_meta
ALTER TABLE documents_meta
    ADD COLUMN data_category     VARCHAR(50) REFERENCES data_categories(id),
    ADD COLUMN contains_pii      BOOLEAN DEFAULT false,
    ADD COLUMN retention_expires_at TIMESTAMPTZ,
    ADD COLUMN data_subject_refs TEXT[];    -- References to data subjects

CREATE INDEX idx_docs_retention ON documents_meta(retention_expires_at)
    WHERE retention_expires_at IS NOT NULL;

-- Data subjects (for access and deletion requests)
CREATE TABLE data_subjects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref    VARCHAR(255) UNIQUE NOT NULL,  -- External ID (customer no., etc.)
    pseudonym       VARCHAR(100) UNIQUE,           -- Pseudonymized identifier
    datasets        UUID[],                        -- Linked datasets
    qdrant_point_ids TEXT[],                       -- Linked Qdrant vector IDs
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_data_subjects_ref ON data_subjects(external_ref);
CREATE INDEX idx_data_subjects_pseudonym ON data_subjects(pseudonym);

-- Deletion request tracking (Art. 17 GDPR)
CREATE TABLE deletion_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    data_subject_id UUID REFERENCES data_subjects(id),
    request_date    TIMESTAMPTZ DEFAULT now(),
    status          VARCHAR(20) DEFAULT 'pending',  -- pending, processing, completed, blocked
    blocked_reason  TEXT,                            -- e.g. statutory retention obligation
    completed_at    TIMESTAMPTZ,
    deleted_records JSONB,                          -- What was deleted (evidence)
    processed_by    VARCHAR(100)                    -- Agent or admin
);

-- Extend audit log with privacy fields
ALTER TABLE agent_access_log
    ADD COLUMN contains_pii      BOOLEAN DEFAULT false,
    ADD COLUMN purpose            VARCHAR(100),
    ADD COLUMN legal_basis        VARCHAR(100),
    ADD COLUMN data_category      VARCHAR(50),
    ADD COLUMN fields_redacted    TEXT[];

-- PII scan results (detection log)
CREATE TABLE pii_scan_log (
    id              BIGSERIAL PRIMARY KEY,
    source          VARCHAR(500),
    scan_date       TIMESTAMPTZ DEFAULT now(),
    entities_found  JSONB,        -- {"email": 3, "phone": 1, "iban": 2}
    action_taken    VARCHAR(20),  -- mask, pseudonymize, encrypt_and_store, block
    classification  VARCHAR(50),
    dataset_id      UUID REFERENCES datasets(id)
);

-- View: all datasets with an expiring retention period
CREATE VIEW v_expiring_data AS
SELECT
    'dataset' AS source_type,
    id,
    name AS title,
    data_category,
    contains_pii,
    retention_expires_at,
    retention_expires_at - now() AS time_remaining
FROM datasets
WHERE retention_expires_at IS NOT NULL
  AND retention_expires_at < now() + INTERVAL '30 days'
UNION ALL
SELECT
    'document' AS source_type,
    id,
    title,
    data_category,
    contains_pii,
    retention_expires_at,
    retention_expires_at - now() AS time_remaining
FROM documents_meta
WHERE retention_expires_at IS NOT NULL
  AND retention_expires_at < now() + INTERVAL '30 days'
ORDER BY retention_expires_at ASC;
