-- init-db/007_pii_vault.sql
-- Sealed Vault: secure storage of original PII data
-- Separate schema with Row-Level Security

-- ── Schema + role ──────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS pii_vault;

-- DB role for vault access (only the MCP server may assume this role)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_vault_reader') THEN
        CREATE ROLE mcp_vault_reader NOLOGIN;
    END IF;
END
$$;

-- Grant access to pb_admin (the application user)
GRANT USAGE ON SCHEMA pii_vault TO mcp_vault_reader;

-- ── Tables ─────────────────────────────────────────────────

-- Original content (plaintext + detected PII entities)
CREATE TABLE pii_vault.original_content (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents_meta(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    original_text   TEXT NOT NULL,
    pii_entities    JSONB NOT NULL DEFAULT '[]',
    stored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    retention_expires_at TIMESTAMPTZ NOT NULL,
    data_category   VARCHAR(50) REFERENCES data_categories(id),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX idx_vault_content_retention
    ON pii_vault.original_content(retention_expires_at);
CREATE INDEX idx_vault_content_document
    ON pii_vault.original_content(document_id);

-- Pseudonym mapping (for traceability + Art. 17 deletion)
CREATE TABLE pii_vault.pseudonym_mapping (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents_meta(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    pseudonym       VARCHAR(20) NOT NULL,
    entity_type     VARCHAR(50) NOT NULL,
    salt            VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_vault_mapping_document
    ON pii_vault.pseudonym_mapping(document_id);
CREATE INDEX idx_vault_mapping_pseudonym
    ON pii_vault.pseudonym_mapping(pseudonym);

-- Separate audit log only for vault access
CREATE TABLE pii_vault.vault_access_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        VARCHAR(200) NOT NULL,
    document_id     UUID NOT NULL,
    chunk_index     INT,
    purpose         VARCHAR(100) NOT NULL,
    token_hash      VARCHAR(64) NOT NULL,
    accessed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_vault_access_agent
    ON pii_vault.vault_access_log(agent_id, accessed_at);
CREATE INDEX idx_vault_access_document
    ON pii_vault.vault_access_log(document_id);

-- Project salts (deterministic per project)
CREATE TABLE pii_vault.project_salts (
    project_id      VARCHAR(100) PRIMARY KEY REFERENCES projects(id),
    salt            VARCHAR(200) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at      TIMESTAMPTZ
);

-- ── Row-Level Security ─────────────────────────────────────

ALTER TABLE pii_vault.original_content ENABLE ROW LEVEL SECURITY;
ALTER TABLE pii_vault.pseudonym_mapping ENABLE ROW LEVEL SECURITY;
ALTER TABLE pii_vault.vault_access_log  ENABLE ROW LEVEL SECURITY;
ALTER TABLE pii_vault.project_salts     ENABLE ROW LEVEL SECURITY;

-- Audit log: even the table owner may not delete
ALTER TABLE pii_vault.vault_access_log FORCE ROW LEVEL SECURITY;

-- Policy: only mcp_vault_reader and the DB owner (pb_admin) may access
CREATE POLICY vault_content_read ON pii_vault.original_content
    FOR SELECT TO mcp_vault_reader USING (true);

CREATE POLICY vault_content_insert ON pii_vault.original_content
    FOR INSERT TO mcp_vault_reader WITH CHECK (true);

CREATE POLICY vault_content_delete ON pii_vault.original_content
    FOR DELETE TO mcp_vault_reader USING (true);

CREATE POLICY vault_mapping_read ON pii_vault.pseudonym_mapping
    FOR SELECT TO mcp_vault_reader USING (true);

CREATE POLICY vault_mapping_insert ON pii_vault.pseudonym_mapping
    FOR INSERT TO mcp_vault_reader WITH CHECK (true);

CREATE POLICY vault_mapping_delete ON pii_vault.pseudonym_mapping
    FOR DELETE TO mcp_vault_reader USING (true);

CREATE POLICY vault_access_log_insert ON pii_vault.vault_access_log
    FOR INSERT TO mcp_vault_reader WITH CHECK (true);

CREATE POLICY vault_access_log_read ON pii_vault.vault_access_log
    FOR SELECT TO mcp_vault_reader USING (true);

-- Salts are append-only: no UPDATE/DELETE to preserve pseudonym integrity
CREATE POLICY vault_salts_read ON pii_vault.project_salts
    FOR SELECT TO mcp_vault_reader USING (true);

CREATE POLICY vault_salts_insert ON pii_vault.project_salts
    FOR INSERT TO mcp_vault_reader WITH CHECK (true);

-- Grant permissions
GRANT SELECT, INSERT, DELETE ON pii_vault.original_content TO mcp_vault_reader;
GRANT SELECT, INSERT, DELETE ON pii_vault.pseudonym_mapping TO mcp_vault_reader;
GRANT SELECT, INSERT ON pii_vault.vault_access_log TO mcp_vault_reader;
GRANT SELECT, INSERT ON pii_vault.project_salts TO mcp_vault_reader;

-- ── View: orphaned vault entries ───────────────────────────
CREATE OR REPLACE VIEW pii_vault.v_orphaned_content AS
SELECT oc.id, oc.document_id, oc.chunk_index, oc.stored_at
FROM pii_vault.original_content oc
LEFT JOIN documents_meta dm ON dm.id = oc.document_id
WHERE dm.id IS NULL;

-- ── View: expired vault entries ────────────────────────────
CREATE OR REPLACE VIEW pii_vault.v_expired_vault_content AS
SELECT id, document_id, chunk_index, retention_expires_at, data_category
FROM pii_vault.original_content
WHERE retention_expires_at <= now();
