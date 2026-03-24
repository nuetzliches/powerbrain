-- 012_context_layers.sql: Add context layer support (L0/L1/L2)
--
-- Each ingested document gets three layers:
--   L0 = Abstract (~100 tokens) for quick relevance filtering
--   L1 = Overview (~1-2k tokens) for understanding scope and key points
--   L2 = Full content chunks (existing behavior)
--
-- L0 and L1 are stored as additional Qdrant points.
-- This migration tracks their point IDs in documents_meta.

-- Track Qdrant point IDs for L0 and L1 layers per document
ALTER TABLE documents_meta ADD COLUMN IF NOT EXISTS l0_point_id UUID;
ALTER TABLE documents_meta ADD COLUMN IF NOT EXISTS l1_point_id UUID;

COMMENT ON COLUMN documents_meta.l0_point_id IS 'Qdrant point ID for L0 abstract';
COMMENT ON COLUMN documents_meta.l1_point_id IS 'Qdrant point ID for L1 overview';
