-- migrations/035_correction_extraction_hints.sql
-- dprompt-115: Self-growing extraction hints
-- Pattern confidence (dprompt-114) IS the bootstrap seed.
-- Successful corrections store extraction_hints → next match uses hints → confidence grows.

-- Add extraction_hints JSONB column for self-growing extraction knowledge
ALTER TABLE correction_signals
  ADD COLUMN IF NOT EXISTS extraction_hints JSONB DEFAULT NULL;

-- Add seed_confidence to track confidence at hint creation time
ALTER TABLE correction_signals
  ADD COLUMN IF NOT EXISTS seed_confidence FLOAT DEFAULT NULL;

-- Index for hint retrieval during correction gate
CREATE INDEX IF NOT EXISTS idx_correction_signals_hints
  ON correction_signals (confidence DESC)
  WHERE extraction_hints IS NOT NULL;
