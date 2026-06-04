-- Migration 064: Revert migration 056 Pitfall 11 violations
-- Migration 056 injected keyword lists and multi-sentence descriptions into intent_classes.
-- GLiNER2 benchmark (test_gliner2_intent_v2.py): V1 clean descriptions = 80%/QUERY 100%.
-- Keyword-enriched descriptions = 65% (WORSE). Reverting to validated V1 form.
-- Only reverts rows that migration 056 set (refined_by = 'claude-code-fix-1.1').
-- Rows customized by users (refined_by = 'user') are untouched.

UPDATE intent_classes
SET
    description = 'User is providing new information or facts',
    version     = version + 1,
    refined_at  = NOW(),
    updated_at  = NOW(),
    refined_by  = 'bootstrap'
WHERE intent_name = 'STATEMENT'
  AND refined_by  = 'claude-code-fix-1.1';

UPDATE intent_classes
SET
    description = 'User is correcting or updating previous information',
    version     = version + 1,
    refined_at  = NOW(),
    updated_at  = NOW(),
    refined_by  = 'bootstrap'
WHERE intent_name = 'CORRECTION'
  AND refined_by  = 'claude-code-fix-1.1';
