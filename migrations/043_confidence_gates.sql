-- Migration: confidence_gates
-- Date: 2026-05-25
-- Purpose: Store per-user adaptive confidence gate thresholds

-- Table: confidence_gates
-- Stores per-user dynamic confidence thresholds for intent classification (dprompt-144).
-- Adjusted based on user correction feedback via the re_embedder.

CREATE TABLE IF NOT EXISTS confidence_gates (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL UNIQUE,

    -- Current threshold for this user (starts at 0.70)
    -- If GLiNER2 confidence >= threshold, trust classification
    -- If < threshold, query negation_patterns fallback
    threshold FLOAT DEFAULT 0.70,

    -- Track when this was last adjusted for optimization
    adjusted_at TIMESTAMP DEFAULT now(),
    created_at TIMESTAMP DEFAULT now()
);

-- Also update negation_patterns to add pattern_hash field if not exists
-- This is used by dprompt-144 to track patterns by hash instead of plaintext
ALTER TABLE negation_patterns
ADD COLUMN IF NOT EXISTS pattern_hash VARCHAR(16);

-- Create index for pattern_hash lookups
CREATE INDEX IF NOT EXISTS idx_negation_pattern_hash ON negation_patterns (user_id, pattern_hash);
