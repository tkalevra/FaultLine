-- migrations/033_correction_signal_evaluations.sql
-- Correction signal candidate tracking for growth/learning via re_embedder
-- Follows same pattern as ontology_evaluations for rel_type growth

CREATE TABLE IF NOT EXISTS correction_signal_evaluations (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    candidate_pattern TEXT NOT NULL,
    pattern_type VARCHAR(50),
    first_text_snippet TEXT,
    occurrence_count INT DEFAULT 1,
    first_seen_at TIMESTAMP DEFAULT now(),
    last_seen_at TIMESTAMP DEFAULT now(),
    re_embedder_decision VARCHAR(20),
    re_embedder_confidence FLOAT,
    approved_pattern_id INT REFERENCES correction_signals(id),
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE(user_id, candidate_pattern)
);

CREATE INDEX IF NOT EXISTS idx_correction_evals_decision ON correction_signal_evaluations(re_embedder_decision);
CREATE INDEX IF NOT EXISTS idx_correction_evals_pending ON correction_signal_evaluations(re_embedder_decision, occurrence_count);
CREATE INDEX IF NOT EXISTS idx_correction_evals_last_seen ON correction_signal_evaluations(last_seen_at);
