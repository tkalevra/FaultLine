-- Migration: negation_patterns
-- Date: 2026-05-25
-- Purpose: Create tables for intent classification fallback and confidence feedback

-- Table: negation_patterns
-- Stores learned patterns for intent classification when GLiNER2 confidence is low.
-- Used by dprompt-144 Layer 2 fallback for RETRACTION/CORRECTION intent detection.

CREATE TABLE IF NOT EXISTS negation_patterns (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL,

    -- Pattern to match: "I don't have", "forget about", "no longer", etc.
    pattern_text TEXT NOT NULL,

    -- Intent override: "retraction", "denial", "removal", "negation"
    -- Maps to filter routing: RETRACTION, CORRECTION, etc.
    negation_type VARCHAR(50) NOT NULL,

    -- Source: "gliner_override" (GLiNER2 low-conf fallback),
    --         "correction_feedback" (user corrected GLiNER2),
    --         "re_embedder_inferred" (learned from correction patterns)
    learned_from VARCHAR(50) DEFAULT 'correction_feedback',

    -- Confidence: starts 0.4 (novel), grows with confirmations
    confidence FLOAT DEFAULT 0.4,

    -- Reinforcement counters (for self-adjusting gate)
    confirmed_count INT DEFAULT 0,      -- User affirmed this pattern
    contradicted_count INT DEFAULT 0,   -- User corrected/rejected this pattern

    -- Metadata
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    -- Prevent duplicates per user
    UNIQUE(user_id, pattern_text, negation_type)
);

-- Table: intent_confidence_feedback
-- Stores user feedback on intent classification confidence for self-adjusting gate.
-- Used by dprompt-144 Layer 3 to dynamically adjust per-user confidence thresholds.

CREATE TABLE IF NOT EXISTS intent_confidence_feedback (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL,

    -- Binned confidence ranges for gate adjustment
    -- Examples: "0.5-0.6", "0.6-0.7", "0.7-0.8", "0.8-0.9"
    confidence_bin VARCHAR(10),

    -- Feedback: "correction" (user corrected), "confirmation" (user accepted)
    feedback_type VARCHAR(50),

    -- Count towards adjustment algorithm
    count INT DEFAULT 1,

    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    UNIQUE(user_id, confidence_bin, feedback_type)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_negation_user_confidence ON negation_patterns (user_id, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_user_bin ON intent_confidence_feedback (user_id, confidence_bin);
