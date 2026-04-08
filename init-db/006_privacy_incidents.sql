-- ============================================================
--  Privacy incidents (GDPR Art. 33/34)
--  Migration: 006_privacy_incidents.sql
--
--  Purpose: logs potential privacy breaches discovered by
--  LLMs, PII scanners, or manual review. Forms the evidence
--  path for Art. 5(2) (accountability) and the 72-hour
--  notification process under Art. 33.
--
--  IMPORTANT: this table must NEVER be emptied.
--  Even "closed" incidents remain as evidence.
-- ============================================================

CREATE TYPE incident_status AS ENUM (
    'detected',          -- Detected automatically or manually, not yet assessed
    'under_review',      -- Data protection officer / admin is reviewing
    'contained',         -- Data access locked, further dissemination stopped
    'notified_authority',-- Notification to supervisory authority sent (Art. 33)
    'notified_subject',  -- Data subject informed (Art. 34)
    'resolved',          -- Closed, no notification required or already reported
    'false_positive'     -- Review found no actual breach
);

CREATE TYPE incident_source AS ENUM (
    'llm_detection',     -- LLM detected unanonymized PII in the context
    'pii_scanner',       -- Presidio scan during re-indexing
    'agent_report',      -- Agent reported via submit_feedback or explicit report
    'manual_audit',      -- Human review
    'retention_check'    -- Retention cleanup discovered orphaned PII data
);

CREATE TABLE privacy_incidents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Detection
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    detected_by         VARCHAR(200) NOT NULL,   -- Agent ID, 'presidio', 'retention_job', etc.
    source              incident_source NOT NULL,
    status              incident_status NOT NULL DEFAULT 'detected',

    -- What was detected
    description         TEXT NOT NULL,           -- Free text: exactly what was found
    affected_data       JSONB NOT NULL DEFAULT '{}',
    -- Example:
    -- {"dataset_ids": ["uuid1"], "document_ids": ["uuid2"],
    --  "qdrant_point_ids": ["id1","id2"],
    --  "pii_types": ["EMAIL_ADDRESS","PERSON"],
    --  "estimated_subject_count": 3}
    pii_types_found     TEXT[],                  -- Redundant for fast queries
    data_category       VARCHAR(50) REFERENCES data_categories(id),

    -- Affected data subjects
    data_subject_ids    UUID[],                  -- References to data_subjects.id
    estimated_subjects  INTEGER,                 -- When exact mapping is not possible

    -- Containment
    contained_at        TIMESTAMPTZ,
    containment_actions JSONB,
    -- Example:
    -- {"quarantined_datasets": ["uuid1"],
    --  "revoked_access": ["agent-x"],
    --  "qdrant_points_deleted": 5}

    -- Notification assessment (Art. 33/34)
    -- Notifiable when: the breach is likely to result in a risk to rights/freedoms
    notifiable_risk     BOOLEAN,                 -- NULL = not yet assessed
    risk_assessment     TEXT,                    -- Rationale
    authority_notified_at TIMESTAMPTZ,           -- NULL if not reported
    authority_ref       VARCHAR(200),            -- Authority reference number
    subject_notified_at TIMESTAMPTZ,

    -- Closure
    resolved_at         TIMESTAMPTZ,
    resolved_by         VARCHAR(200),
    resolution_notes    TEXT,

    -- Audit trail (append-only via trigger)
    status_history      JSONB NOT NULL DEFAULT '[]',
    -- Array of: {"ts": "...", "from": "...", "to": "...", "by": "...", "note": "..."}

    -- Link to deletion requests
    deletion_request_id UUID REFERENCES deletion_requests(id)
);

CREATE INDEX idx_incidents_status    ON privacy_incidents(status);
CREATE INDEX idx_incidents_detected  ON privacy_incidents(detected_at);
CREATE INDEX idx_incidents_source    ON privacy_incidents(source);
CREATE INDEX idx_incidents_pii_types ON privacy_incidents USING gin(pii_types_found);
CREATE INDEX idx_incidents_notifiable ON privacy_incidents(notifiable_risk)
    WHERE notifiable_risk = true AND authority_notified_at IS NULL;

-- Trigger: automatically record status changes in history
CREATE OR REPLACE FUNCTION track_incident_status()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        NEW.status_history = OLD.status_history || jsonb_build_object(
            'ts',   now(),
            'from', OLD.status::text,
            'to',   NEW.status::text,
            'by',   current_setting('app.current_user', true)
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_incident_status_history
    BEFORE UPDATE ON privacy_incidents
    FOR EACH ROW EXECUTE FUNCTION track_incident_status();

-- View: open incidents that might endanger the 72-hour notification deadline
-- (Art. 33: notification within 72 hours of becoming aware)
CREATE VIEW v_incidents_requiring_attention AS
SELECT
    id,
    detected_at,
    EXTRACT(EPOCH FROM (now() - detected_at)) / 3600 AS hours_since_detection,
    status,
    source,
    description,
    pii_types_found,
    notifiable_risk,
    CASE
        WHEN EXTRACT(EPOCH FROM (now() - detected_at)) / 3600 > 48
             AND status NOT IN ('resolved', 'false_positive', 'notified_authority')
        THEN 'CRITICAL: less than 24h until the 72h deadline'
        WHEN EXTRACT(EPOCH FROM (now() - detected_at)) / 3600 > 24
             AND status = 'detected'
        THEN 'WARNING: incident not yet assessed'
        ELSE 'ok'
    END AS frist_warnung
FROM privacy_incidents
WHERE status NOT IN ('resolved', 'false_positive', 'notified_authority')
ORDER BY detected_at ASC;
