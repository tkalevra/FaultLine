-- Entity attributes: scalar values for entities
-- Replaces storing scalar facts (age, born_on, etc.) in the facts table
-- entity_id references entities(id, user_id)

CREATE TABLE IF NOT EXISTS entity_attributes (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    attribute    TEXT NOT NULL,
    value_text   TEXT,
    value_int    INTEGER,
    value_float  DOUBLE PRECISION,
    value_date   DATE,
    provenance   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, entity_id, attribute),
    FOREIGN KEY (entity_id, user_id) REFERENCES entities(id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entity_attributes_user ON entity_attributes (user_id);
CREATE INDEX IF NOT EXISTS idx_entity_attributes_entity ON entity_attributes (user_id, entity_id);

-- Migrate existing scalar facts into entity_attributes
INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, value_int, provenance)
SELECT
    user_id,
    subject_id,
    rel_type,
    object_id,
    CASE WHEN object_id ~ '^\d+$' THEN object_id::integer ELSE NULL END,
    provenance
FROM facts
WHERE rel_type IN ('age', 'born_on', 'born_in', 'nationality', 'occupation', 'has_gender')
AND user_id IS NOT NULL
ON CONFLICT (user_id, entity_id, attribute) DO NOTHING;

-- Mark migrated scalar facts as superseded
UPDATE facts SET superseded_at = now()
WHERE rel_type IN ('age', 'born_on', 'born_in', 'nationality', 'occupation', 'has_gender')
AND user_id IS NOT NULL
AND superseded_at IS NULL;
