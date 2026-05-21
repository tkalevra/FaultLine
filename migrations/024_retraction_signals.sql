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

-- Seed initial retraction signals
INSERT INTO retraction_signals (signal, signal_category, priority, language) VALUES
    -- Explicit retractions (high priority, low false positive rate)
    ('forget', 'explicit', 95, 'en'),
    ('delete', 'explicit', 95, 'en'),
    ('remove', 'explicit', 95, 'en'),
    ('retract', 'explicit', 95, 'en'),
    ('erase', 'explicit', 95, 'en'),
    ('that was wrong', 'explicit', 90, 'en'),
    ('that is wrong', 'explicit', 90, 'en'),
    ('that is incorrect', 'explicit', 90, 'en'),
    ('no longer', 'explicit', 85, 'en'),
    ('remove from memory', 'explicit', 90, 'en'),

    -- Implicit negations (medium priority, safer with semantic gate)
    ('is not my', 'implicit_negation', 60, 'en'),
    ('is not a', 'implicit_negation', 60, 'en'),
    ('is not the', 'implicit_negation', 50, 'en'),
    ('is not', 'implicit_negation', 40, 'en'),
    ('isn''t my', 'implicit_negation', 60, 'en'),
    ('isn''t a', 'implicit_negation', 60, 'en'),
    ('am not', 'implicit_negation', 50, 'en'),
    ('was not', 'implicit_negation', 50, 'en'),
    ('wasn''t', 'implicit_negation', 50, 'en'),
    ('were not', 'implicit_negation', 50, 'en'),
    ('wrong about', 'implicit_negation', 70, 'en'),
    ('not my', 'implicit_negation', 65, 'en'),
    ('not a', 'implicit_negation', 55, 'en'),

    -- Corrections (medium priority)
    ('actually', 'correction', 70, 'en'),
    ('i was wrong', 'correction', 80, 'en'),
    ('i meant', 'correction', 75, 'en'),
    ('should be', 'correction', 60, 'en'),
    ('should have been', 'correction', 70, 'en'),
    ('scratch that', 'correction', 85, 'en')
ON CONFLICT (signal, language) DO NOTHING;

-- Indexes for quick signal lookup and outcome analysis
CREATE INDEX IF NOT EXISTS idx_retraction_signals_priority
    ON retraction_signals(language, priority DESC);
CREATE INDEX IF NOT EXISTS idx_retraction_outcomes_method
    ON retraction_outcomes(retraction_method, was_correct);
