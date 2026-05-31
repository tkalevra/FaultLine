-- Migration: enhance_intent_classes_descriptions
-- Date: 2026-05-29
-- Purpose: Enhanced intent class descriptions with preference signals and self-identification context
-- Respects dprompt-152: Better descriptions improve GLiNER2 zero-shot classification accuracy

-- Enhancement Goal:
-- 1. STATEMENT: Add preference keywords (goes by, prefers to be called, known as, call me)
--    Include self-correction context (clarifying/updating own statements)
--    Clarify negation handling ("No, my name is..." is still STATEMENT, not RETRACTION)
-- 2. CORRECTION: Distinguish from STATEMENT by emphasizing acknowledgment of prior state
--    ("Actually...", "I meant...", "I was wrong...") shows awareness of previous fact

-- Update STATEMENT intent with enhanced description
UPDATE intent_classes
SET
    description = 'User is providing new information, facts, context, or personal details to be learned and stored. Includes self-identification and preference signals. Keywords: my name, I have, I work at, I live in, I own, my spouse is, I am, I went to, goes by, prefers to be called, known as, call me, you can call me, I prefer. Also includes self-corrections and clarifications: No/actually, my name is..., I meant to say..., let me clarify. Action: ingests new facts or preference statements into knowledge graph.',
    version = version + 1,
    refined_at = NOW(),
    updated_at = NOW(),
    refined_by = 'claude-code-fix-1.1'
WHERE intent_name = 'STATEMENT'
  AND refined_by != 'user';

-- Update CORRECTION intent with enhanced description to distinguish from STATEMENT
UPDATE intent_classes
SET
    description = 'User is explicitly providing updated or corrected information that overrides or supersedes previously stated facts. Distinguishing feature: user acknowledges prior state and indicates change. Keywords: actually, correction, I meant, I was wrong, it''s not, change that, that was wrong, not the one, I lied, I misspoke, scratch that, let me correct myself, I confused, I mixed up. Implies awareness of prior fact and intentional change. Action: replaces/updates existing facts with Class A override (confidence 1.0).',
    version = version + 1,
    refined_at = NOW(),
    updated_at = NOW(),
    refined_by = 'claude-code-fix-1.1'
WHERE intent_name = 'CORRECTION'
  AND refined_by != 'user';

-- Verification: Log the enhancements
DO $$
DECLARE
    stmt_desc TEXT;
    corr_desc TEXT;
BEGIN
    SELECT description INTO stmt_desc FROM intent_classes WHERE intent_name = 'STATEMENT';
    SELECT description INTO corr_desc FROM intent_classes WHERE intent_name = 'CORRECTION';

    RAISE NOTICE 'Migration 056: Intent class descriptions enhanced';
    RAISE NOTICE 'STATEMENT: % chars', LENGTH(stmt_desc);
    RAISE NOTICE 'CORRECTION: % chars', LENGTH(corr_desc);
END $$;
