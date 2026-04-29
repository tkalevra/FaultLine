DROP TABLE IF EXISTS entity_aliases;
DROP TABLE IF EXISTS entities;

CREATE TABLE entities (
    id          TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'unknown',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, user_id)
);

CREATE INDEX idx_entities_user ON entities (user_id);

CREATE TABLE entity_aliases (
    id           SERIAL PRIMARY KEY,
    entity_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    alias        TEXT NOT NULL,
    is_preferred BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, alias),
    FOREIGN KEY (entity_id, user_id) REFERENCES entities(id, user_id) ON DELETE CASCADE
);

CREATE INDEX idx_entity_aliases_user ON entity_aliases (user_id);
CREATE INDEX idx_entity_aliases_entity ON entity_aliases (entity_id, user_id);
CREATE INDEX idx_entity_aliases_preferred ON entity_aliases (user_id, entity_id) WHERE is_preferred = true;

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