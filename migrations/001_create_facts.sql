-- FaultLine WGM — initial schema

CREATE TABLE IF NOT EXISTS facts (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT        NOT NULL DEFAULT 'anonymous',
    subject_id  TEXT        NOT NULL,
    object_id   TEXT        NOT NULL,
    rel_type    TEXT        NOT NULL,
    provenance  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, subject_id, object_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_facts_pair
    ON facts (subject_id, object_id);

CREATE INDEX IF NOT EXISTS idx_facts_user_subject
    ON facts (user_id, subject_id);

CREATE INDEX IF NOT EXISTS idx_facts_user_object
    ON facts (user_id, object_id);

-- Track which facts have been synced to Qdrant
ALTER TABLE facts ADD COLUMN IF NOT EXISTS qdrant_synced BOOLEAN DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_facts_unsynced
    ON facts (qdrant_synced, user_id)
    WHERE qdrant_synced = false;

CREATE OR REPLACE FUNCTION lowercase_facts_columns()
RETURNS TRIGGER AS $$
BEGIN
    NEW.subject_id := lower(NEW.subject_id);
    NEW.object_id := lower(NEW.object_id);
    NEW.rel_type := lower(NEW.rel_type);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Safe migration: only create triggers if they don't exist (drop and recreate)
DROP TRIGGER IF EXISTS lowercase_facts_before_insert ON facts;
CREATE TRIGGER lowercase_facts_before_insert
BEFORE INSERT ON facts
FOR EACH ROW
EXECUTE FUNCTION lowercase_facts_columns();

DROP TRIGGER IF EXISTS lowercase_facts_before_update ON facts;
CREATE TRIGGER lowercase_facts_before_update
BEFORE UPDATE ON facts
FOR EACH ROW
EXECUTE FUNCTION lowercase_facts_columns();

CREATE TABLE IF NOT EXISTS pending_types (
    id          SERIAL PRIMARY KEY,
    rel_type    TEXT        NOT NULL,
    subject_id  TEXT,
    object_id   TEXT,
    flagged_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);