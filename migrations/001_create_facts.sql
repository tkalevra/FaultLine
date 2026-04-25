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

CREATE TABLE IF NOT EXISTS pending_types (
    id          SERIAL PRIMARY KEY,
    rel_type    TEXT        NOT NULL,
    subject_id  TEXT,
    object_id   TEXT,
    flagged_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
