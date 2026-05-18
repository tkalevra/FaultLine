-- Migration: Add unified_confidence column to facts table
-- Supports LLMOutputValidator unified confidence scoring (frequency + llm confidence blend)
-- Default to existing confidence value for backward compatibility

ALTER TABLE facts
  ADD COLUMN IF NOT EXISTS unified_confidence DOUBLE PRECISION DEFAULT 1.0;

-- Add index for efficient filtering by confidence scores
CREATE INDEX IF NOT EXISTS idx_facts_unified_confidence
  ON facts(user_id, unified_confidence DESC)
  WHERE archived_at IS NULL;

-- Index for querying low-confidence facts that need re-evaluation
CREATE INDEX IF NOT EXISTS idx_facts_low_unified_confidence
  ON facts(user_id, unified_confidence)
  WHERE unified_confidence < 0.6 AND archived_at IS NULL;

-- Add unified_confidence to staged_facts as well
ALTER TABLE staged_facts
  ADD COLUMN IF NOT EXISTS unified_confidence DOUBLE PRECISION DEFAULT 0.8;

-- Index for staged facts confidence tracking
CREATE INDEX IF NOT EXISTS idx_staged_facts_unified_confidence
  ON staged_facts(user_id, unified_confidence DESC);

-- Comment for documentation
COMMENT ON COLUMN facts.unified_confidence IS
  'Blended confidence score from LLMOutputValidator: (frequency/threshold)*0.5 + llm_confidence*0.5';

COMMENT ON COLUMN staged_facts.unified_confidence IS
  'Blended confidence score for staged facts before promotion to facts table';
