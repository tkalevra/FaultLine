-- Phase 1: Surrogate key migration
-- Adds valid_from/valid_until to entity_aliases for bitemporal support (Phase 2 prep).
-- Does NOT migrate existing data rows — existing facts with display-name subject_id/object_id
-- will be handled by a separate data migration script run manually after deployment.

-- 1. Add valid_from/valid_until to entity_aliases (nullable, Phase 2 prep)
ALTER TABLE entity_aliases
  ADD COLUMN IF NOT EXISTS valid_from  TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ DEFAULT NULL;

-- 2. Enforce exactly one is_preferred per entity per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_aliases_one_preferred
  ON entity_aliases (user_id, entity_id)
  WHERE is_preferred = true;

-- 3. Add FK constraint from facts to entities on subject_id/object_id
--    (deferred — applied after data migration, not in this file)
--    Placeholder comment only:
-- ALTER TABLE facts ADD CONSTRAINT fk_facts_subject FOREIGN KEY (subject_id) REFERENCES entities(id);
-- ALTER TABLE facts ADD CONSTRAINT fk_facts_object  FOREIGN KEY (object_id)  REFERENCES entities(id);
