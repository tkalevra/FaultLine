-- Add correction_behavior to rel_types table
ALTER TABLE rel_types
  ADD COLUMN IF NOT EXISTS correction_behavior TEXT NOT NULL DEFAULT 'supersede'
  CHECK (correction_behavior IN ('hard_delete', 'supersede', 'immutable'));

-- Seed correction behaviors for existing rel_types
UPDATE rel_types SET correction_behavior = 'hard_delete'
  WHERE rel_type IN ('also_known_as', 'pref_name');

UPDATE rel_types SET correction_behavior = 'immutable'
  WHERE rel_type IN ('born_in', 'born_on', 'parent_of', 'child_of', 'sibling_of');

UPDATE rel_types SET correction_behavior = 'supersede'
  WHERE rel_type IN (
    'lives_at', 'works_for', 'spouse', 'owns', 'located_at',
    'likes', 'dislikes', 'prefers', 'is_a', 'has_pet',
    'occupation', 'nationality', 'educated_at'
  );

-- Add superseded_at to facts for soft-delete tracking
ALTER TABLE facts
  ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ DEFAULT NULL;

-- Index for re-embedder deletion loop
CREATE INDEX IF NOT EXISTS idx_facts_superseded
  ON facts (qdrant_synced, superseded_at)
  WHERE superseded_at IS NOT NULL AND qdrant_synced = false;
