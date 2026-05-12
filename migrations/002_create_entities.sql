-- Entities table (idempotent)
CREATE TABLE IF NOT EXISTS entities (
    id          TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'unknown',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_entities_user ON entities (user_id);
