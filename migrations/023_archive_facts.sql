-- dprompt-90: Add archive support to facts table
-- Allows non-destructive supersession: user corrections archive old facts
-- Historical queries still work via include_archived parameter

ALTER TABLE facts ADD COLUMN archived_at TIMESTAMP NULL DEFAULT NULL;

-- Index for default query behavior (archived_at IS NULL)
CREATE INDEX IF NOT EXISTS idx_facts_active
  ON facts(user_id, archived_at)
  WHERE archived_at IS NULL;

-- Index for historical queries (archived_at IS NOT NULL)
CREATE INDEX IF NOT EXISTS idx_facts_archived
  ON facts(user_id, archived_at)
  WHERE archived_at IS NOT NULL;
