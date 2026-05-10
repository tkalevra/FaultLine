-- Phase 2: Bitemporal columns
-- Adds valid_from/valid_until (valid time) to facts and entity_attributes.
-- recorded_at replaces created_at semantics going forward (transaction time).
-- hard_delete_flag added for explicit erasure pipeline.

-- facts: valid time columns
ALTER TABLE facts
  ADD COLUMN IF NOT EXISTS valid_from       TIMESTAMPTZ DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS valid_until      TIMESTAMPTZ DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS hard_delete_flag BOOLEAN     NOT NULL DEFAULT false;

-- Index: active facts (valid_until IS NULL = currently true)
CREATE INDEX IF NOT EXISTS idx_facts_active
  ON facts (user_id, subject_id, rel_type)
  WHERE valid_until IS NULL AND superseded_at IS NULL;

-- Index: historical facts (valid_until IS NOT NULL = bounded in time)
CREATE INDEX IF NOT EXISTS idx_facts_historical
  ON facts (user_id, valid_until)
  WHERE valid_until IS NOT NULL;

-- Index: hard delete queue
CREATE INDEX IF NOT EXISTS idx_facts_hard_delete
  ON facts (hard_delete_flag)
  WHERE hard_delete_flag = true;

-- entity_attributes: valid time columns (if table exists)
ALTER TABLE entity_attributes
  ADD COLUMN IF NOT EXISTS valid_from  TIMESTAMPTZ DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ DEFAULT NULL;

-- Index: active attributes
CREATE INDEX IF NOT EXISTS idx_entity_attributes_active
  ON entity_attributes (user_id, entity_id, attribute)
  WHERE valid_until IS NULL;
