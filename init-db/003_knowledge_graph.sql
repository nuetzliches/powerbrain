-- ============================================================
--  Wissensdatenbank – Knowledge Graph (Apache AGE)
--  Migration: 003_knowledge_graph.sql
--
--  Apache AGE erweitert PostgreSQL um einen Property-Graph.
--  Damit können Beziehungen zwischen Entitäten (Projekte,
--  Technologien, Personen, Regeln) traversiert werden —
--  etwas, das reine Vektorsuche nicht kann.
--
--  Voraussetzung: PostgreSQL-Image mit AGE-Extension
--  Image: apache/age
-- ============================================================

-- Extension laden
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Graph erstellen
SELECT create_graph('knowledge');

-- ── Vertex-Labels (Knotentypen) ────────────────────────────

SELECT create_vlabel('knowledge', 'Project');
SELECT create_vlabel('knowledge', 'Technology');
SELECT create_vlabel('knowledge', 'Actor');
SELECT create_vlabel('knowledge', 'Document');
SELECT create_vlabel('knowledge', 'Rule');
SELECT create_vlabel('knowledge', 'Concept');

-- ── Edge-Labels (Beziehungstypen) ──────────────────────────

SELECT create_elabel('knowledge', 'USES');
SELECT create_elabel('knowledge', 'WORKS_ON');
SELECT create_elabel('knowledge', 'HAS_ROLE');
SELECT create_elabel('knowledge', 'BELONGS_TO');
SELECT create_elabel('knowledge', 'DESCRIBES');
SELECT create_elabel('knowledge', 'APPLIES_TO');
SELECT create_elabel('knowledge', 'RELATED_TO');
SELECT create_elabel('knowledge', 'DEPENDS_ON');

-- ── Beispieldaten ──────────────────────────────────────────

SELECT * FROM cypher('knowledge', $$
  CREATE (:Project {name: 'Wissensdatenbank', phase: 'Aufbau', classification: 'internal'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Project {name: 'API-Gateway', phase: 'Produktion', classification: 'confidential'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Technology {name: 'Qdrant', category: 'database', version: '1.12'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Technology {name: 'PostgreSQL', category: 'database', version: '16'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Technology {name: 'OPA', category: 'policy_engine'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Technology {name: 'FastAPI', category: 'framework'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  MATCH (p:Project {name: 'Wissensdatenbank'}), (t:Technology {name: 'Qdrant'})
  CREATE (p)-[:USES {since: '2026-03', purpose: 'Vektorsuche'}]->(t)
$$) AS (e agtype);

SELECT * FROM cypher('knowledge', $$
  MATCH (p:Project {name: 'Wissensdatenbank'}), (t:Technology {name: 'PostgreSQL'})
  CREATE (p)-[:USES {since: '2026-03', purpose: 'Strukturierte Daten'}]->(t)
$$) AS (e agtype);

SELECT * FROM cypher('knowledge', $$
  MATCH (p:Project {name: 'Wissensdatenbank'}), (t:Technology {name: 'OPA'})
  CREATE (p)-[:USES {since: '2026-03', purpose: 'Regelwerk'}]->(t)
$$) AS (e agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Concept {name: 'DSGVO', domain: 'compliance'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Concept {name: 'PII-Erkennung', domain: 'datenschutz'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  MATCH (c1:Concept {name: 'PII-Erkennung'}), (c2:Concept {name: 'DSGVO'})
  CREATE (c1)-[:RELATED_TO {relation: 'implementiert_anforderung_von'}]->(c2)
$$) AS (e agtype);

-- ── Views für schnellen Zugriff ────────────────────────────

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
