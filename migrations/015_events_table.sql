-- 015_events_table.sql
-- Temporal events separation from static facts.
-- Events (birthdays, anniversaries, appointments) have recurrence rules
-- and time-aware retrieval semantics distinct from relationship facts.

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,   -- UUID or "user"
    object_id TEXT NOT NULL,    -- date string or entity name
    event_type TEXT NOT NULL,   -- born_on, met_on, anniversary_on, appointment_on, etc.
    occurs_on TEXT NOT NULL,    -- raw date string as extracted
    recurrence TEXT,            -- "yearly", "monthly", "once", null
    confidence FLOAT DEFAULT 0.8,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE(user_id, subject_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_events_user_subject ON events(user_id, subject_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(user_id, event_type);
