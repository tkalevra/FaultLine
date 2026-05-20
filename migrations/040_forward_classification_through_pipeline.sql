-- Migration 040: Forward pre-computed classification through pipeline (SOLUTION-CLASSIFY-FORWARD)
-- Stores storage_type, is_hierarchy_rel, taxonomies computed during ingest
-- so query can filter facts by detected scope without re-computing

ALTER TABLE facts ADD COLUMN IF NOT EXISTS storage_type TEXT;
ALTER TABLE facts ADD COLUMN IF NOT EXISTS is_hierarchy_rel BOOLEAN DEFAULT false;
ALTER TABLE facts ADD COLUMN IF NOT EXISTS taxonomies TEXT[] DEFAULT '{}';

ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS storage_type TEXT;
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS is_hierarchy_rel BOOLEAN DEFAULT false;
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS taxonomies TEXT[] DEFAULT '{}';

-- Index for efficient taxonomy filtering
CREATE INDEX IF NOT EXISTS idx_facts_taxonomies
    ON facts USING GIN(taxonomies);

CREATE INDEX IF NOT EXISTS idx_staged_facts_taxonomies
    ON staged_facts USING GIN(taxonomies);

-- Comment for documentation
COMMENT ON COLUMN facts.storage_type IS 'Pre-computed classification: scalar, relational, hierarchical, unknown_staging';
COMMENT ON COLUMN facts.is_hierarchy_rel IS 'True if this rel_type traverses hierarchy vs graph';
COMMENT ON COLUMN facts.taxonomies IS 'Pre-computed taxonomies this fact belongs to (family, work, medical, etc.)';

COMMENT ON COLUMN staged_facts.storage_type IS 'Pre-computed classification: scalar, relational, hierarchical, unknown_staging';
COMMENT ON COLUMN staged_facts.is_hierarchy_rel IS 'True if this rel_type traverses hierarchy vs graph';
COMMENT ON COLUMN staged_facts.taxonomies IS 'Pre-computed taxonomies this fact belongs to (family, work, medical, etc.)';
