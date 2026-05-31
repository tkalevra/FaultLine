-- Migration 057: Preference Patterns Table for Layer 2c Fallback Detection
-- Purpose: Store subject-agnostic preference signal patterns to boost STATEMENT confidence
-- Created: 2026-05-29
-- Idempotent: Safe to run multiple times

CREATE TABLE IF NOT EXISTS preference_patterns (
    id SERIAL PRIMARY KEY,
    pattern_text VARCHAR(255) NOT NULL UNIQUE,
    signal_type VARCHAR(50) NOT NULL,
    intent_name VARCHAR(50) NOT NULL DEFAULT 'STATEMENT',
    base_confidence FLOAT NOT NULL DEFAULT 0.90,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    created_by VARCHAR(255) DEFAULT 'bootstrap',

    -- Column comments for self-documentation
    CONSTRAINT chk_base_confidence CHECK (base_confidence >= 0.0 AND base_confidence <= 1.0),
    CONSTRAINT chk_signal_type CHECK (signal_type IN ('preference', 'alias', 'identity_correction')),
    CONSTRAINT chk_intent_name CHECK (intent_name IN ('STATEMENT', 'QUERY', 'CORRECTION', 'RETRACTION'))
);

-- Index for active pattern lookups by pattern text
CREATE INDEX IF NOT EXISTS idx_preference_patterns_active
ON preference_patterns(pattern_text)
WHERE is_active = true;

-- Index for filtering by signal type
CREATE INDEX IF NOT EXISTS idx_preference_patterns_by_type
ON preference_patterns(signal_type)
WHERE is_active = true;

-- Bootstrap preference patterns - subject-agnostic preference signal detection
-- These patterns are metadata-driven: re_embedder can add new patterns via INSERT
INSERT INTO preference_patterns (pattern_text, signal_type, intent_name, base_confidence, is_active, created_by)
VALUES
    ('i prefer to be called', 'preference', 'STATEMENT', 0.95, true, 'bootstrap'),
    ('goes by', 'alias', 'STATEMENT', 0.90, true, 'bootstrap'),
    ('known as', 'alias', 'STATEMENT', 0.90, true, 'bootstrap'),
    ('call me', 'preference', 'STATEMENT', 0.85, true, 'bootstrap'),
    ('should be called', 'preference', 'STATEMENT', 0.88, true, 'bootstrap'),
    ('actual name', 'identity_correction', 'STATEMENT', 0.92, true, 'bootstrap'),
    ('real name', 'identity_correction', 'STATEMENT', 0.92, true, 'bootstrap'),
    ('i go by', 'alias', 'STATEMENT', 0.90, true, 'bootstrap')
ON CONFLICT (pattern_text)
DO UPDATE SET
    signal_type = EXCLUDED.signal_type,
    intent_name = EXCLUDED.intent_name,
    base_confidence = EXCLUDED.base_confidence,
    is_active = EXCLUDED.is_active,
    updated_at = now()
WHERE preference_patterns.created_by = 'bootstrap';

-- Table comment (PostgreSQL documentation)
COMMENT ON TABLE preference_patterns IS
    'Subject-agnostic preference signal patterns for Layer 2c fallback detection. Patterns match conversation context to boost STATEMENT intent confidence when GLiNER2 confidence is below threshold.';

COMMENT ON COLUMN preference_patterns.pattern_text IS
    'Exact substring pattern to match in conversation text (case-insensitive matching handled at query time)';

COMMENT ON COLUMN preference_patterns.signal_type IS
    'Category of preference signal: preference (name preference), alias (known as/goes by), identity_correction (real/actual name)';

COMMENT ON COLUMN preference_patterns.intent_name IS
    'Intent classification this pattern represents (always STATEMENT for preference patterns)';

COMMENT ON COLUMN preference_patterns.base_confidence IS
    'Confidence boost applied when pattern matches (0.0-1.0 range, used to override GLiNER2 low-confidence results)';

COMMENT ON COLUMN preference_patterns.is_active IS
    'Soft-delete flag: false deactivates pattern without removing data (allows re_embedder to refine patterns)';

COMMENT ON COLUMN preference_patterns.created_by IS
    'Source identifier: bootstrap (seeded), re_embedder (auto-learned), user_id (explicit additions)';
