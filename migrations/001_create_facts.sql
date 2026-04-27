-- FaultLine WGM — initial schema

CREATE TABLE IF NOT EXISTS facts (
    id          SERIAL PRIMARY KEY,
    subject_id  TEXT        NOT NULL,
    object_id   TEXT        NOT NULL,
    rel_type    TEXT        NOT NULL,
    provenance  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_facts_pair
    ON facts (subject_id, object_id);

CREATE OR REPLACE FUNCTION lowercase_facts_columns()
RETURNS TRIGGER AS $$
BEGIN
    NEW.subject_id := lower(NEW.subject_id);
    NEW.object_id := lower(NEW.object_id);
    NEW.rel_type := lower(NEW.rel_type);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER lowercase_facts_before_insert
BEFORE INSERT ON facts
FOR EACH ROW
EXECUTE FUNCTION lowercase_facts_columns();

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