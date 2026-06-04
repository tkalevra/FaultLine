-- Migration 066: intent_pattern_cache
-- Date: 2026-06-04
-- Purpose: Replaces negation_patterns ledger with evictable TTL cache for Layer 2a pattern matching
-- Seeds start at confidence=0.50-0.70 (below 0.70 query floor for ambiguous), must earn confidence via confirmed use
-- contradicted_count decrements confidence and halves TTL — misfiring patterns decay
-- confirmed_count >= 10 promotes to is_permanent=true (survives eviction)
-- Part of dprompt-153: intent classification pipeline repair (Change Set B2)
-- NOTE: This table is in the public schema (shared across users). NOT added to per-user schema template.

CREATE TABLE IF NOT EXISTS intent_pattern_cache (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    pattern_text TEXT NOT NULL,
    intent_type VARCHAR(50) NOT NULL,        -- retraction | correction | ambiguous
    negation_type VARCHAR(50),               -- compat with negation_patterns; retraction|correction
    confidence FLOAT NOT NULL DEFAULT 0.60 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    confirmed_count INT NOT NULL DEFAULT 0,
    contradicted_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT now(),
    last_fired_at TIMESTAMP,
    expires_at TIMESTAMP NOT NULL DEFAULT (now() + INTERVAL '3 days'),
    is_permanent BOOLEAN NOT NULL DEFAULT false,
    min_context_chars INT NOT NULL DEFAULT 0,
    requires_replacement_clause BOOLEAN NOT NULL DEFAULT false,
    learned_from VARCHAR(100) DEFAULT 'bootstrap',
    UNIQUE (user_id, pattern_text, intent_type)
);

-- Lookup index: covers active patterns (permanent or not yet expired)
-- Note: expires_at > now() cannot be used in partial index (now() is not immutable)
-- Runtime query filters on (is_permanent OR expires_at > now()) — index covers full user+confidence scan
CREATE INDEX IF NOT EXISTS idx_intent_pattern_cache_lookup
    ON intent_pattern_cache (user_id, confidence DESC);

-- Eviction index: allows efficient delete/update of non-permanent expired patterns
CREATE INDEX IF NOT EXISTS idx_intent_pattern_cache_eviction
    ON intent_pattern_cache (expires_at)
    WHERE is_permanent = false;

-- Bootstrap seeds: lower confidence, shorter TTL than legacy negation_patterns
-- 'is not' classified as ambiguous at 0.50 — below query floor, cannot fire without confirmation
-- 'forget', 'delete', 'remove': explicit action verbs — start at 0.65 (above floor after 1 confirmation)
-- Correction seeds require min_context_chars to prevent bare-token matches

INSERT INTO intent_pattern_cache (user_id, pattern_text, intent_type, negation_type, confidence, min_context_chars, requires_replacement_clause, learned_from)
VALUES
    ('__global__', 'forget', 'retraction', 'retraction', 0.65, 0, false, 'bootstrap_153'),
    ('__global__', 'delete', 'retraction', 'retraction', 0.65, 0, false, 'bootstrap_153'),
    ('__global__', 'remove', 'retraction', 'retraction', 0.65, 0, false, 'bootstrap_153'),
    ('__global__', 'erase', 'retraction', 'retraction', 0.60, 0, false, 'bootstrap_153'),
    ('__global__', 'no longer', 'retraction', 'retraction', 0.60, 4, false, 'bootstrap_153'),
    ('__global__', 'not anymore', 'retraction', 'retraction', 0.60, 4, false, 'bootstrap_153'),
    ('__global__', 'is not', 'ambiguous', NULL, 0.50, 0, false, 'bootstrap_153'),
    ('__global__', 'is not a', 'ambiguous', NULL, 0.50, 0, false, 'bootstrap_153'),
    ('__global__', 'i meant', 'correction', 'correction', 0.65, 4, false, 'bootstrap_153'),
    ('__global__', 'that is wrong', 'correction', 'correction', 0.70, 8, false, 'bootstrap_153'),
    ('__global__', 'actually', 'correction', 'correction', 0.55, 6, false, 'bootstrap_153'),
    ('__global__', 'wrong', 'correction', 'correction', 0.55, 4, false, 'bootstrap_153'),
    ('__global__', 'incorrect', 'correction', 'correction', 0.55, 4, false, 'bootstrap_153'),
    ('__global__', 'typo', 'correction', 'correction', 0.60, 0, false, 'bootstrap_153'),
    ('__global__', 'changed my mind', 'correction', 'correction', 0.70, 0, false, 'bootstrap_153')
ON CONFLICT (user_id, pattern_text, intent_type) DO NOTHING;
