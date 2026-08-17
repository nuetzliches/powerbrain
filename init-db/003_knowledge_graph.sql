-- ============================================================
--  Knowledge base – Knowledge Graph (Apache AGE)
--  Migration: 003_knowledge_graph.sql
--
--  Apache AGE extends PostgreSQL with a property graph,
--  enabling traversal of relationships between entities
--  (projects, technologies, people, rules) — something that
--  pure vector search cannot do.
--
--  Requirement: PostgreSQL image with AGE extension
--  Image: apache/age
-- ============================================================

-- Load extension
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- ── Graph + labels ─────────────────────────────────────────
--
-- AGE has no IF NOT EXISTS for create_graph/create_vlabel/create_elabel,
-- so guard against its catalog (ag_graph.name, ag_label.name+graph).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'knowledge') THEN
        PERFORM ag_catalog.create_graph('knowledge');
    END IF;
END
$$;

-- The ::cstring casts are required, not cosmetic: create_vlabel/create_elabel
-- take cstring parameters. Bare string literals work because "unknown"
-- coerces to cstring, but a TEXT loop variable does not -- without the cast
-- this fails with "function ag_catalog.create_vlabel(unknown, text) does not
-- exist".
DO $$
DECLARE
    graph_oid OID;
    lbl       TEXT;
BEGIN
    SELECT graphid INTO graph_oid FROM ag_catalog.ag_graph WHERE name = 'knowledge';

    -- Vertex labels (node types)
    FOREACH lbl IN ARRAY ARRAY[
        'Project', 'Technology', 'Actor', 'Document', 'Rule', 'Concept'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label
            WHERE name = lbl AND graph = graph_oid
        ) THEN
            PERFORM ag_catalog.create_vlabel('knowledge'::cstring, lbl::cstring);
        END IF;
    END LOOP;

    -- Edge labels (relationship types)
    FOREACH lbl IN ARRAY ARRAY[
        'USES', 'WORKS_ON', 'HAS_ROLE', 'BELONGS_TO',
        'DESCRIBES', 'APPLIES_TO', 'RELATED_TO', 'DEPENDS_ON'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label
            WHERE name = lbl AND graph = graph_oid
        ) THEN
            PERFORM ag_catalog.create_elabel('knowledge'::cstring, lbl::cstring);
        END IF;
    END LOOP;
END
$$;

-- ── No example data ────────────────────────────────────────
--
-- This file used to seed demo nodes here (Projects 'knowledge base' and
-- 'API-Gateway', Technologies Qdrant/PostgreSQL/OPA/FastAPI, Concepts
-- GDPR/'PII detection') via plain cypher CREATE. Because db-migrate re-runs
-- every init-db file on each deploy and CREATE has no uniqueness check,
-- those statements never errored -- they silently inserted another copy
-- every time. The MATCH ... CREATE edge statements made it worse than
-- linear: on run N they match N copies on each side and create N^2 edges.
-- Live this had reached 25 copies per node and 16575 USES edges instead of 3.
--
-- Keep this section empty. The graph is populated by ingestion, and demo
-- content does not belong in a migration that re-runs on every deploy.

-- ── Views for fast access ──────────────────────────────────

CREATE OR REPLACE VIEW v_project_technologies AS
SELECT *
FROM cypher('knowledge', $$
  MATCH (p:Project)-[u:USES]->(t:Technology)
  RETURN p.name AS project, t.name AS technology, t.category AS category, u.purpose AS purpose
$$) AS (project agtype, technology agtype, category agtype, purpose agtype);

CREATE OR REPLACE VIEW v_concept_relations AS
SELECT *
FROM cypher('knowledge', $$
  MATCH (c1:Concept)-[r:RELATED_TO]->(c2:Concept)
  RETURN c1.name AS from_concept, c2.name AS to_concept, r.relation AS relation
$$) AS (from_concept agtype, to_concept agtype, relation agtype);
