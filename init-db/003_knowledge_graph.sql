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

-- Create graph
SELECT create_graph('knowledge');

-- ── Vertex labels (node types) ─────────────────────────────

SELECT create_vlabel('knowledge', 'Project');
SELECT create_vlabel('knowledge', 'Technology');
SELECT create_vlabel('knowledge', 'Actor');
SELECT create_vlabel('knowledge', 'Document');
SELECT create_vlabel('knowledge', 'Rule');
SELECT create_vlabel('knowledge', 'Concept');

-- ── Edge labels (relationship types) ───────────────────────

SELECT create_elabel('knowledge', 'USES');
SELECT create_elabel('knowledge', 'WORKS_ON');
SELECT create_elabel('knowledge', 'HAS_ROLE');
SELECT create_elabel('knowledge', 'BELONGS_TO');
SELECT create_elabel('knowledge', 'DESCRIBES');
SELECT create_elabel('knowledge', 'APPLIES_TO');
SELECT create_elabel('knowledge', 'RELATED_TO');
SELECT create_elabel('knowledge', 'DEPENDS_ON');

-- ── Example data ───────────────────────────────────────────

SELECT * FROM cypher('knowledge', $$
  CREATE (:Project {name: 'knowledge base', phase: 'setup', classification: 'internal'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Project {name: 'API-Gateway', phase: 'production', classification: 'confidential'})
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
  MATCH (p:Project {name: 'knowledge base'}), (t:Technology {name: 'Qdrant'})
  CREATE (p)-[:USES {since: '2026-03', purpose: 'vector search'}]->(t)
$$) AS (e agtype);

SELECT * FROM cypher('knowledge', $$
  MATCH (p:Project {name: 'knowledge base'}), (t:Technology {name: 'PostgreSQL'})
  CREATE (p)-[:USES {since: '2026-03', purpose: 'structured data'}]->(t)
$$) AS (e agtype);

SELECT * FROM cypher('knowledge', $$
  MATCH (p:Project {name: 'knowledge base'}), (t:Technology {name: 'OPA'})
  CREATE (p)-[:USES {since: '2026-03', purpose: 'policy set'}]->(t)
$$) AS (e agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Concept {name: 'GDPR', domain: 'compliance'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  CREATE (:Concept {name: 'PII detection', domain: 'privacy'})
$$) AS (v agtype);

SELECT * FROM cypher('knowledge', $$
  MATCH (c1:Concept {name: 'PII detection'}), (c2:Concept {name: 'GDPR'})
  CREATE (c1)-[:RELATED_TO {relation: 'implements_requirement_of'}]->(c2)
$$) AS (e agtype);

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
