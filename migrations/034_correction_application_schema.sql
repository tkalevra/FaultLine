-- migrations/034_correction_application_schema.sql
-- dprompt-115: Correction Application — schema additions
-- Adds columns needed for CLASS A temporal versioning + pattern learning

-- =========================================================================
-- 1. correction_signals: Make patterns per-user + add learning fields
-- =========================================================================

-- Add user_id so patterns are per-user (not global)
ALTER TABLE correction_signals
  ADD COLUMN IF NOT EXISTS user_id TEXT;

-- Add success_count for pattern learning feedback loop
ALTER TABLE correction_signals
  ADD COLUMN IF NOT EXISTS success_count INTEGER DEFAULT 0;

-- Add last_applied_at to track when pattern was last used successfully
ALTER TABLE correction_signals
  ADD COLUMN IF NOT EXISTS last_applied_at TIMESTAMPTZ DEFAULT NULL;

-- Index for per-user pattern lookup (dprompt-115 correction gate)
CREATE INDEX IF NOT EXISTS idx_correction_signals_user_confidence
  ON correction_signals (user_id, confidence DESC, created_at DESC)
  WHERE user_id IS NOT NULL;

-- =========================================================================
-- 2. entity_attributes: Add temporal versioning + confidence columns
-- =========================================================================

-- corrected_at: timestamp of the most recent correction
ALTER TABLE entity_attributes
  ADD COLUMN IF NOT EXISTS corrected_at TIMESTAMPTZ DEFAULT NULL;

-- superseded_at: when this row was replaced by a newer correction
ALTER TABLE entity_attributes
  ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ DEFAULT NULL;

-- confidence: reliability score for this scalar value
ALTER TABLE entity_attributes
  ADD COLUMN IF NOT EXISTS confidence FLOAT DEFAULT NULL;

-- Index: active (non-superseded) attributes
CREATE INDEX IF NOT EXISTS idx_entity_attributes_active_corrected
  ON entity_attributes (user_id, entity_id, attribute)
  WHERE superseded_at IS NULL AND valid_until IS NULL;

-- =========================================================================
-- 3. Update existing entity_attributes rows: set valid_from to created_at
--    for rows that lack valid_from (pre-011 migration data)
-- =========================================================================

UPDATE entity_attributes
SET valid_from = created_at
WHERE valid_from IS NULL;
