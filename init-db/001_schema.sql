-- ============================================================
--  Knowledge base – PostgreSQL schema initialization
-- ============================================================

-- Classification levels
CREATE TABLE classifications (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(50) UNIQUE NOT NULL,
    level       INTEGER NOT NULL,           -- 0=public, 1=internal, 2=confidential, 3=restricted
    description TEXT,
    access_policy VARCHAR(100)              -- Reference to OPA policy
);

INSERT INTO classifications (name, level, description, access_policy) VALUES
('public',       0, 'Freely accessible to all agents',  'pb.access.public'),
('internal',     1, 'Internal agents only',             'pb.access.internal'),
('confidential', 2, 'Restricted access',                'pb.access.confidential'),
('restricted',   3, 'Strictly controlled',              'pb.access.restricted');

-- Datasets (imported CSV, JSON etc.)
CREATE TABLE datasets (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             VARCHAR(255) NOT NULL,
    description      TEXT,
    schema_def       JSONB,                  -- JSON Schema of the dataset
    source           VARCHAR(500),           -- Origin (file path, URL, etc.)
    source_type      VARCHAR(50),            -- csv, json, sql_dump, git_repo
    classification   VARCHAR(50) REFERENCES classifications(name) DEFAULT 'internal',
    project          VARCHAR(100),
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_datasets_project ON datasets(project);
CREATE INDEX idx_datasets_classification ON datasets(classification);

-- Individual rows of a dataset
CREATE TABLE dataset_rows (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id  UUID REFERENCES datasets(id) ON DELETE CASCADE,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_dataset_rows_dataset ON dataset_rows(dataset_id);
CREATE INDEX idx_dataset_rows_data ON dataset_rows USING GIN(data);

-- Document metadata (reference to Qdrant vectors)
CREATE TABLE documents_meta (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title               VARCHAR(500) NOT NULL,
    source              VARCHAR(500),
    source_type         VARCHAR(50),          -- csv, json, git, sql_dump
    qdrant_collection   VARCHAR(100),
    chunk_count         INTEGER DEFAULT 0,
    classification      VARCHAR(50) REFERENCES classifications(name) DEFAULT 'internal',
    project             VARCHAR(100),
    metadata            JSONB,                -- Additional metadata
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_documents_meta_project ON documents_meta(project);
CREATE INDEX idx_documents_meta_classification ON documents_meta(classification);

-- Audit log for agent access
CREATE TABLE agent_access_log (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        VARCHAR(100) NOT NULL,
    agent_role      VARCHAR(50),
    resource_type   VARCHAR(50),              -- dataset, document, rule, policy
    resource_id     VARCHAR(255),
    action          VARCHAR(50),              -- search, query, ingest, check_policy
    policy_result   VARCHAR(20),              -- allow, deny
    policy_reason   TEXT,
    request_context JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_audit_agent ON agent_access_log(agent_id);
CREATE INDEX idx_audit_time ON agent_access_log(created_at);
CREATE INDEX idx_audit_result ON agent_access_log(policy_result);

-- Project management
CREATE TABLE projects (
    id          VARCHAR(100) PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);
