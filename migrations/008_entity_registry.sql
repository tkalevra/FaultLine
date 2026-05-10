-- Entity aliases table (idempotent)
CREATE TABLE IF NOT EXISTS entity_aliases (
    id           SERIAL PRIMARY KEY,
    entity_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    alias        TEXT NOT NULL,
    is_preferred BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, alias),
    FOREIGN KEY (entity_id, user_id) REFERENCES entities(id, user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_user ON entity_aliases (user_id);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases (entity_id, user_id);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_preferred
    ON entity_aliases (user_id, entity_id) WHERE is_preferred = true;

-- Migrate existing also_known_as facts into entity_aliases
INSERT INTO entities (id, user_id, entity_type)
SELECT DISTINCT subject_id, user_id, 'unknown'
FROM facts WHERE user_id IS NOT NULL
ON CONFLICT (id, user_id) DO NOTHING;

INSERT INTO entities (id, user_id, entity_type)
SELECT DISTINCT object_id, user_id, 'unknown'
FROM facts WHERE user_id IS NOT NULL
ON CONFLICT (id, user_id) DO NOTHING;

INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred)
SELECT subject_id, user_id, object_id, is_preferred_label
FROM facts WHERE rel_type = 'also_known_as' AND user_id IS NOT NULL
ON CONFLICT (user_id, alias) DO NOTHING;

INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred)
SELECT subject_id, user_id, object_id, true
FROM facts WHERE rel_type = 'pref_name' AND user_id IS NOT NULL
ON CONFLICT (user_id, alias) DO UPDATE SET is_preferred = true;
