-- Phase 4: Staged facts table for short-term memory
-- Class B and C facts land here first.
-- Re-embedder promotes Class B to facts when confirmed_count >= 3.
-- Class C expires after 30 days without confirmation.

CREATE TABLE IF NOT EXISTS staged_facts (
    id               BIGSERIAL PRIMARY KEY,
    user_id          UUID        NOT NULL,
    subject_id       UUID        NOT NULL,
    object_id        UUID        NOT NULL,
    rel_type         TEXT        NOT NULL,
    fact_class       TEXT        NOT NULL CHECK (fact_class IN ('B', 'C')),
    provenance       TEXT        NOT NULL DEFAULT 'llm_inferred',
    confidence       FLOAT       NOT NULL DEFAULT 0.6,
    confirmed_count  INT         NOT NULL DEFAULT 0,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days',
    promoted_at      TIMESTAMPTZ DEFAULT NULL,
    qdrant_synced    BOOLEAN     NOT NULL DEFAULT false,
    UNIQUE (user_id, subject_id, object_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_staged_facts_user
    ON staged_facts (user_id);

CREATE INDEX IF NOT EXISTS idx_staged_facts_promote
    ON staged_facts (user_id, fact_class, confirmed_count)
    WHERE promoted_at IS NULL AND fact_class = 'B';

CREATE INDEX IF NOT EXISTS idx_staged_facts_expire
    ON staged_facts (expires_at)
    WHERE promoted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_staged_facts_unsynced
    ON staged_facts (qdrant_synced, user_id)
    WHERE qdrant_synced = false;

-- Add provenance column to facts table for actor-aware tracking
ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS fact_provenance TEXT NOT NULL DEFAULT 'user_stated';

-- Add fact_class to facts for promoted facts (always 'A' or promoted 'B')
ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS fact_class TEXT NOT NULL DEFAULT 'A';

-- Lowercase trigger for staged_facts
CREATE OR REPLACE FUNCTION lowercase_staged_facts()
RETURNS TRIGGER AS $$
BEGIN
    NEW.rel_type = LOWER(TRIM(NEW.rel_type));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lowercase_staged_facts_before_insert ON staged_facts;
CREATE TRIGGER lowercase_staged_facts_before_insert
    BEFORE INSERT ON staged_facts
    FOR EACH ROW EXECUTE FUNCTION lowercase_staged_facts();

DROP TRIGGER IF EXISTS lowercase_staged_facts_before_update ON staged_facts;
CREATE TRIGGER lowercase_staged_facts_before_update
    BEFORE UPDATE ON staged_facts
    FOR EACH ROW EXECUTE FUNCTION lowercase_staged_facts();
