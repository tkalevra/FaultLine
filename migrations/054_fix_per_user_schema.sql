-- Migration: Fix per-user schema isolation for negation_patterns and intent_confidence_feedback
-- Date: 2026-05-29
-- Purpose: Add user_id columns and fix unique constraints for proper schema isolation

-- Fix negation_patterns table in public schema
ALTER TABLE IF EXISTS public.negation_patterns
ADD COLUMN IF NOT EXISTS user_id UUID NOT NULL DEFAULT gen_random_uuid();

ALTER TABLE IF EXISTS public.negation_patterns
ADD COLUMN IF NOT EXISTS contradicted_count INT DEFAULT 0;

ALTER TABLE IF EXISTS public.negation_patterns
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

-- Drop old constraint and recreate with user_id
ALTER TABLE IF EXISTS public.negation_patterns
DROP CONSTRAINT IF EXISTS negation_patterns_pkey CASCADE;

ALTER TABLE IF EXISTS public.negation_patterns
DROP CONSTRAINT IF EXISTS negation_patterns_pattern_text_negation_type_key;

ALTER TABLE IF EXISTS public.negation_patterns
ADD PRIMARY KEY (id);

ALTER TABLE IF EXISTS public.negation_patterns
ADD UNIQUE (user_id, pattern_text, negation_type);

-- Fix indexes
DROP INDEX IF EXISTS idx_negation_user_confidence;
CREATE INDEX idx_negation_user_confidence ON public.negation_patterns (user_id, confidence DESC);

DROP INDEX IF EXISTS idx_negation_pattern_hash;
CREATE INDEX idx_negation_pattern_hash ON public.negation_patterns (pattern_hash);

-- Fix intent_confidence_feedback table in public schema
ALTER TABLE IF EXISTS public.intent_confidence_feedback
ADD COLUMN IF NOT EXISTS user_id UUID NOT NULL DEFAULT gen_random_uuid();

ALTER TABLE IF EXISTS public.intent_confidence_feedback
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

-- Drop old constraint and recreate with user_id
ALTER TABLE IF EXISTS public.intent_confidence_feedback
DROP CONSTRAINT IF EXISTS intent_confidence_feedback_pkey CASCADE;

ALTER TABLE IF EXISTS public.intent_confidence_feedback
DROP CONSTRAINT IF EXISTS intent_confidence_feedback_confidence_bin_feedback_type_key;

ALTER TABLE IF EXISTS public.intent_confidence_feedback
ADD PRIMARY KEY (id);

ALTER TABLE IF EXISTS public.intent_confidence_feedback
ADD UNIQUE (user_id, confidence_bin, feedback_type);

-- Fix indexes
DROP INDEX IF EXISTS idx_feedback_user_bin;
CREATE INDEX idx_feedback_user_bin ON public.intent_confidence_feedback (user_id, confidence_bin);

DROP INDEX IF EXISTS idx_intent_confidence_feedback_bin;
CREATE INDEX idx_intent_confidence_feedback_bin ON public.intent_confidence_feedback (confidence_bin);
