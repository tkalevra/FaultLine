-- dprompt-106: Metadata-driven retraction signal detection
-- Replaces hardcoded frozenset with database-backed, learning-enabled system

-- Retraction signals metadata table (like rel_types, but for retraction patterns)
CREATE TABLE IF NOT EXISTS retraction_signals (
    id SERIAL PRIMARY KEY,
    signal TEXT NOT NULL,
    signal_category VARCHAR(50) NOT NULL,  -- 'explicit', 'implicit_negation', 'correction'
    language VARCHAR(5) NOT NULL DEFAULT 'en',
    priority INT DEFAULT 50,  -- 0-100: higher = checked first
    false_positive_rate FLOAT DEFAULT 0.0,  -- Empirically measured (0.0-1.0)
    false_negative_rate FLOAT DEFAULT 0.0,
    notes TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(signal, language)
);

-- Learning table: track retraction detection outcomes for continuous improvement
CREATE TABLE IF NOT EXISTS retraction_outcomes (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255),
    original_message TEXT NOT NULL,
    detected_as_retraction BOOLEAN,
    retraction_method VARCHAR(50),  -- 'semantic', 'pattern', 'none'
    detected_confidence FLOAT,
    extracted_subject VARCHAR(255),
    extracted_rel_type VARCHAR(255),
    extracted_old_value TEXT,
    actually_retracted BOOLEAN,
    was_correct BOOLEAN,
    created_at TIMESTAMP DEFAULT now()
);

-- Create indexes separately (PostgreSQL syntax, idempotent)
CREATE INDEX IF NOT EXISTS idx_outcomes_user ON retraction_outcomes(user_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_was_correct ON retraction_outcomes(was_correct);

-- PHASE 5: Start with empty retraction_signals table
-- Signal patterns are discovered purely from Christopher's language patterns
-- via retraction_outcomes learning table, not bootstrap biases
-- Learned patterns (frequency >= 3) accumulate over time at priority > 70
-- Bootstrap signals would mask actual user patterns, so we learn from zero state

-- Indexes for quick signal lookup and outcome analysis
CREATE INDEX IF NOT EXISTS idx_retraction_signals_priority
    ON retraction_signals(language, priority DESC);
CREATE INDEX IF NOT EXISTS idx_retraction_outcomes_method
    ON retraction_outcomes(retraction_method, was_correct);
