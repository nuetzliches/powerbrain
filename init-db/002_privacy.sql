-- ============================================================
--  Wissensdatenbank – Datenschutz-Erweiterung
--  Migration: 002_privacy.sql
-- ============================================================

-- Datenkategorien für DSGVO-Zweckbindung
CREATE TABLE data_categories (
    id              VARCHAR(50) PRIMARY KEY,
    description     TEXT NOT NULL,
    contains_pii    BOOLEAN DEFAULT false,
    legal_basis     VARCHAR(100),           -- Art. 6 DSGVO Rechtsgrundlage
    allowed_purposes TEXT[],                -- Erlaubte Verarbeitungszwecke
    retention_days  INTEGER NOT NULL DEFAULT 365
);

INSERT INTO data_categories (id, description, contains_pii, legal_basis, allowed_purposes, retention_days) VALUES
('customer_data',   'Kundenstammdaten',          true,  'Art. 6 Abs. 1 lit. b (Vertragserfüllung)', ARRAY['support','billing','contract_fulfillment'], 730),
('employee_data',   'Mitarbeiterdaten',           true,  'Art. 6 Abs. 1 lit. b (Arbeitsvertrag)',    ARRAY['hr_management','payroll'], 1095),
('analytics_data',  'Anonymisierte Analysedaten', false, 'Art. 6 Abs. 1 lit. f (Berechtigtes Interesse)', ARRAY['reporting','product_improvement'], 90),
('marketing_data',  'Marketing-Kontakte',         true,  'Art. 6 Abs. 1 lit. a (Einwilligung)',      ARRAY['campaign_management','consent_based_contact'], 365),
('contract_data',   'Vertragsdokumente',          true,  'Art. 6 Abs. 1 lit. b (Vertragserfüllung)', ARRAY['contract_fulfillment','legal'], 1095),
('accounting_data', 'Buchhaltungsdaten',          true,  'Art. 6 Abs. 1 lit. c (Rechtl. Verpflichtung)', ARRAY['accounting','audit'], 3650),
('technical_data',  'Technische Dokumentation',   false, NULL, ARRAY['development','operations'], 365);

-- Datenschutz-Felder zu datasets hinzufügen
ALTER TABLE datasets
    ADD COLUMN data_category     VARCHAR(50) REFERENCES data_categories(id),
    ADD COLUMN contains_pii      BOOLEAN DEFAULT false,
    ADD COLUMN legal_basis        VARCHAR(100),
    ADD COLUMN retention_expires_at TIMESTAMPTZ,
    ADD COLUMN pii_fields         TEXT[],           -- Welche Felder PII enthalten
    ADD COLUMN pseudonymized      BOOLEAN DEFAULT false;

CREATE INDEX idx_datasets_retention ON datasets(retention_expires_at)
    WHERE retention_expires_at IS NOT NULL;
CREATE INDEX idx_datasets_pii ON datasets(contains_pii)
    WHERE contains_pii = true;

-- Datenschutz-Felder zu documents_meta hinzufügen
ALTER TABLE documents_meta
    ADD COLUMN data_category     VARCHAR(50) REFERENCES data_categories(id),
    ADD COLUMN contains_pii      BOOLEAN DEFAULT false,
    ADD COLUMN retention_expires_at TIMESTAMPTZ,
    ADD COLUMN data_subject_refs TEXT[];    -- Referenzen auf betroffene Personen

CREATE INDEX idx_docs_retention ON documents_meta(retention_expires_at)
    WHERE retention_expires_at IS NOT NULL;

-- Betroffene Personen (für Auskunfts- und Löschanfragen)
CREATE TABLE data_subjects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref    VARCHAR(255) UNIQUE NOT NULL,  -- Externe ID (Kunden-Nr., etc.)
    pseudonym       VARCHAR(100) UNIQUE,           -- Pseudonymisierter Identifier
    datasets        UUID[],                        -- Verknüpfte Datensätze
    qdrant_point_ids TEXT[],                       -- Verknüpfte Qdrant-Vektor-IDs
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_data_subjects_ref ON data_subjects(external_ref);
CREATE INDEX idx_data_subjects_pseudonym ON data_subjects(pseudonym);

-- Löschanfragen-Tracking (Art. 17 DSGVO)
CREATE TABLE deletion_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    data_subject_id UUID REFERENCES data_subjects(id),
    request_date    TIMESTAMPTZ DEFAULT now(),
    status          VARCHAR(20) DEFAULT 'pending',  -- pending, processing, completed, blocked
    blocked_reason  TEXT,                            -- z.B. gesetzliche Aufbewahrungspflicht
    completed_at    TIMESTAMPTZ,
    deleted_records JSONB,                          -- Was wurde gelöscht (Nachweis)
    processed_by    VARCHAR(100)                    -- Agent oder Admin
);

-- Audit-Log um Datenschutzfelder erweitern
ALTER TABLE agent_access_log
    ADD COLUMN contains_pii      BOOLEAN DEFAULT false,
    ADD COLUMN purpose            VARCHAR(100),
    ADD COLUMN legal_basis        VARCHAR(100),
    ADD COLUMN data_category      VARCHAR(50),
    ADD COLUMN fields_redacted    TEXT[];

-- PII-Scan-Ergebnisse (Protokoll der Erkennung)
CREATE TABLE pii_scan_log (
    id              BIGSERIAL PRIMARY KEY,
    source          VARCHAR(500),
    scan_date       TIMESTAMPTZ DEFAULT now(),
    entities_found  JSONB,        -- {"email": 3, "phone": 1, "iban": 2}
    action_taken    VARCHAR(20),  -- mask, pseudonymize, encrypt_and_store, block
    classification  VARCHAR(50),
    dataset_id      UUID REFERENCES datasets(id)
);

-- View: Alle Datensätze mit ablaufender Aufbewahrungsfrist
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
