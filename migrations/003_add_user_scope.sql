-- Add user_id to facts (idempotent)
ALTER TABLE facts ADD COLUMN IF NOT EXISTS user_id TEXT;
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_unique_edge
    ON facts (user_id, subject_id, object_id, rel_type);
