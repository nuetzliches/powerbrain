-- ============================================================
--  Datenschutzvorfälle (DSGVO Art. 33/34)
--  Migration: 006_privacy_incidents.sql
--
--  Zweck: Protokolliert potenzielle Datenschutzverletzungen,
--  die durch LLMs, PII-Scanner oder manuelle Prüfung entdeckt
--  wurden. Bildet den Nachweis-Pfad für Art. 5(2) (Rechenschaftspflicht)
--  und den 72h-Meldeweg nach Art. 33.
--
--  WICHTIG: Diese Tabelle darf NIEMALS geleert werden.
--  Auch „geschlossene" Vorfälle bleiben als Nachweis erhalten.
-- ============================================================

CREATE TYPE incident_status AS ENUM (
    'detected',          -- Automatisch oder manuell erkannt, noch nicht bewertet
    'under_review',      -- Datenschutzbeauftragter / Admin prüft
    'contained',         -- Datenzugriff gesperrt, weitere Verbreitung gestoppt
    'notified_authority',-- Meldung an Aufsichtsbehörde erfolgt (Art. 33)
    'notified_subject',  -- Betroffene Person informiert (Art. 34)
    'resolved',          -- Abgeschlossen, kein Meldeerfordernis oder bereits gemeldet
    'false_positive'     -- Prüfung ergab keine tatsächliche Verletzung
);

CREATE TYPE incident_source AS ENUM (
    'llm_detection',     -- LLM hat im Kontext PII erkannt, die nicht anonymisiert war
    'pii_scanner',       -- Presidio-Scan bei Re-Indexierung
    'agent_report',      -- Agent hat über submit_feedback oder expliziten Report gemeldet
    'manual_audit',      -- Menschliche Prüfung
    'retention_check'    -- Retention-Cleanup hat verwaiste PII-Daten entdeckt
);

CREATE TABLE privacy_incidents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Erkennung
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    detected_by         VARCHAR(200) NOT NULL,   -- Agent-ID, 'presidio', 'retention_job', etc.
    source              incident_source NOT NULL,
    status              incident_status NOT NULL DEFAULT 'detected',

    -- Was wurde erkannt
    description         TEXT NOT NULL,           -- Freitext: was genau wurde gefunden
    affected_data       JSONB NOT NULL DEFAULT '{}',
    -- Beispiel:
    -- {"dataset_ids": ["uuid1"], "document_ids": ["uuid2"],
    --  "qdrant_point_ids": ["id1","id2"],
    --  "pii_types": ["EMAIL_ADDRESS","PERSON"],
    --  "estimated_subject_count": 3}
    pii_types_found     TEXT[],                  -- Redundant für schnelle Abfragen
    data_category       VARCHAR(50) REFERENCES data_categories(id),

    -- Betroffene Personen
    data_subject_ids    UUID[],                  -- Referenzen auf data_subjects.id
    estimated_subjects  INTEGER,                 -- Falls exakte Zuordnung nicht möglich

    -- Kontainierung
    contained_at        TIMESTAMPTZ,
    containment_actions JSONB,
    -- Beispiel:
    -- {"quarantined_datasets": ["uuid1"],
    --  "revoked_access": ["agent-x"],
    --  "qdrant_points_deleted": 5}

    -- Meldepflicht-Bewertung (Art. 33/34)
    -- Meldepflichtig wenn: Verletzung wahrscheinlich Risiko für Rechte/Freiheiten
    notifiable_risk     BOOLEAN,                 -- NULL = noch nicht bewertet
    risk_assessment     TEXT,                    -- Begründung
    authority_notified_at TIMESTAMPTZ,           -- Null wenn nicht gemeldet
    authority_ref       VARCHAR(200),            -- Aktenzeichen der Behörde
    subject_notified_at TIMESTAMPTZ,

    -- Abschluss
    resolved_at         TIMESTAMPTZ,
    resolved_by         VARCHAR(200),
    resolution_notes    TEXT,

    -- Audit-Trail (append-only via Trigger)
    status_history      JSONB NOT NULL DEFAULT '[]',
    -- Array von: {"ts": "...", "from": "...", "to": "...", "by": "...", "note": "..."}

    -- Verknüpfung mit Löschanfragen
    deletion_request_id UUID REFERENCES deletion_requests(id)
);

CREATE INDEX idx_incidents_status    ON privacy_incidents(status);
CREATE INDEX idx_incidents_detected  ON privacy_incidents(detected_at);
CREATE INDEX idx_incidents_source    ON privacy_incidents(source);
CREATE INDEX idx_incidents_pii_types ON privacy_incidents USING gin(pii_types_found);
CREATE INDEX idx_incidents_notifiable ON privacy_incidents(notifiable_risk)
    WHERE notifiable_risk = true AND authority_notified_at IS NULL;

-- Trigger: Status-Änderungen automatisch in history schreiben
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

-- View: Offene Vorfälle die die 72h-Meldefrist gefährden könnten
-- (Art. 33: Meldung binnen 72h nach Bekanntwerden)
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
        THEN 'KRITISCH: weniger als 24h bis zur 72h-Frist'
        WHEN EXTRACT(EPOCH FROM (now() - detected_at)) / 3600 > 24
             AND status = 'detected'
        THEN 'WARNUNG: Vorfall noch nicht bewertet'
        ELSE 'ok'
    END AS frist_warnung
FROM privacy_incidents
WHERE status NOT IN ('resolved', 'false_positive', 'notified_authority')
ORDER BY detected_at ASC;
