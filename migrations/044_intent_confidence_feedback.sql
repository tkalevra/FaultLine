-- Migration: intent_confidence_feedback
-- Date: 2026-05-25
-- Purpose: Track user feedback on intent classification confidence for adaptive thresholding (dprompt-144 Phase 3)

-- Table: intent_confidence_feedback
-- Stores per-user feedback on confidence levels (correct/incorrect) to enable re_embedder
-- to adjust per-user confidence gates adaptively. Used by dprompt-144 Phase 3.

CREATE TABLE IF NOT EXISTS intent_confidence_feedback (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL,

    -- Confidence bin: 0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8-1.0
    -- Used to track which confidence ranges have the most user corrections
    confidence_bin VARCHAR(10) NOT NULL,

    -- Feedback type: 'correct' or 'incorrect'
    -- Used to calculate win rate per confidence bin
    feedback_type VARCHAR(20) NOT NULL,

    -- How many times this bin/feedback combination has occurred
    count INT DEFAULT 1,

    created_at TIMESTAMP DEFAULT now(),

    -- Unique constraint on (user_id, confidence_bin, feedback_type)
    -- for ON CONFLICT DO UPDATE pattern in re_embedder
    UNIQUE (user_id, confidence_bin, feedback_type)
);

-- Index for fast user-specific lookups
CREATE INDEX IF NOT EXISTS idx_intent_feedback_user ON intent_confidence_feedback (user_id);
CREATE INDEX IF NOT EXISTS idx_intent_feedback_bin ON intent_confidence_feedback (user_id, confidence_bin);
