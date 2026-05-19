-- migrations/038_scalar_history_temporal_context.sql
-- dBug-055 Phase 1: Schema fix for scalar correction history & temporal facts
-- Adds temporal context tracking + correction sequence history preservation

-- =========================================================================
-- 1. Add temporal_context & temporal_context_resolved_at to entity_attributes
-- =========================================================================

-- temporal_context: Text qualifier from user utterance ("in 4 days", "next Tuesday", etc.)
ALTER TABLE entity_attributes
  ADD COLUMN IF NOT EXISTS temporal_context TEXT;

-- temporal_context_resolved_at: Absolute timestamp when LLM resolved the relative expression
-- Null if expression couldn't be parsed or is non-temporal (e.g., age, height)
ALTER TABLE entity_attributes
  ADD COLUMN IF NOT EXISTS temporal_context_resolved_at TIMESTAMPTZ;

-- =========================================================================
-- 2. History table: non-destructive correction sequence tracking
-- =========================================================================
-- Preserves every corrected-away value with temporal context for pattern learning
-- (dBug-055 Phase 4: enables sequence analysis like 12→14→16 with timing)

CREATE TABLE IF NOT EXISTS entity_attributes_history (
    id                          SERIAL PRIMARY KEY,
    user_id                     TEXT NOT NULL,
    entity_id                   TEXT NOT NULL,
    attribute                   TEXT NOT NULL,
    value_text                  TEXT,
    value_int                   INTEGER,
    value_float                 FLOAT,
    value_date                  DATE,
    provenance                  TEXT,
    confidence                  FLOAT,
    temporal_context            TEXT,
    temporal_context_resolved_at TIMESTAMPTZ,
    valid_from                  TIMESTAMPTZ,
    recorded_at                 TIMESTAMPTZ DEFAULT now()
);

-- Index for fast sequence queries by (entity, attribute, time)
CREATE INDEX IF NOT EXISTS idx_ea_history_entity_attr_time
    ON entity_attributes_history (user_id, entity_id, attribute, recorded_at DESC);
