-- 021_name_conflicts.sql
-- dprompt-32: Entity Name Conflict Resolution System.
-- Non-destructive collision handling: when two entities claim the same preferred name,
-- store the conflict for later resolution by the re-embedder via LLM context.
-- All names are preserved; only preferred status is determined at resolution time.

CREATE TABLE IF NOT EXISTS entity_name_conflicts (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    entity_id_1 UUID NOT NULL,
    entity_name_1 TEXT NOT NULL,
    entity_id_2 UUID NOT NULL,
    entity_name_2 TEXT NOT NULL,
    disputed_name TEXT NOT NULL,
    conflict_type TEXT DEFAULT 'pref_name_collision',
    status TEXT DEFAULT 'pending',
    resolution_method TEXT,
    resolution_detail TEXT,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(user_id, entity_id_1, entity_id_2, disputed_name)
);

CREATE INDEX IF NOT EXISTS idx_conflicts_status
    ON entity_name_conflicts(user_id, status);

CREATE INDEX IF NOT EXISTS idx_conflicts_created
    ON entity_name_conflicts(created_at DESC);
