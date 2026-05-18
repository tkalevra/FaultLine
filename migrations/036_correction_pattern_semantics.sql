-- dprompt-116: Pattern semantics for context filtering (growth-ready, no hard-coded enums)
-- Add semantics column to correction_signals to drive pattern-aware context selection
-- No CONSTRAINT: system learns new semantics types dynamically from patterns

ALTER TABLE correction_signals
ADD COLUMN IF NOT EXISTS semantics TEXT DEFAULT NULL;

-- Index for semantics-based queries (supports growth: new semantics types auto-indexed)
CREATE INDEX IF NOT EXISTS idx_correction_signals_semantics
ON correction_signals(user_id, semantics, confidence DESC);

-- Create pattern_semantic_map table for learned semantic mappings (growth configuration)
CREATE TABLE IF NOT EXISTS pattern_semantic_map (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  pattern TEXT NOT NULL,
  semantics TEXT NOT NULL,
  confidence FLOAT DEFAULT 0.5,
  applicable_rel_types TEXT[] DEFAULT ARRAY[]::TEXT[],
  confirmed_count INT DEFAULT 0,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now(),

  UNIQUE(user_id, pattern)
);

CREATE INDEX IF NOT EXISTS idx_pattern_semantic_map_semantics
ON pattern_semantic_map(user_id, semantics);

-- Heuristic seeding (best-effort, will be overridden by learned mappings)
-- No hard-coded CONSTRAINT — system validates at runtime using learned semantics
INSERT INTO pattern_semantic_map (user_id, pattern, semantics, confidence)
SELECT DISTINCT user_id, pattern,
  CASE
    WHEN pattern ILIKE '%is%not%' OR pattern ILIKE '%from%to%' THEN 'correction'
    WHEN pattern ILIKE '%don%t have%' OR pattern ILIKE '%don%t own%' OR pattern ILIKE '%no more%' THEN 'removal'
    WHEN pattern ILIKE '%call%me%' OR pattern ILIKE '%call%' THEN 'alias'
    ELSE 'unknown'  -- Not hard-coded, will be learned
  END,
  0.6  -- Initial heuristic confidence, will grow with confirmation
FROM correction_signals
WHERE user_id IS NOT NULL
ON CONFLICT (user_id, pattern) DO NOTHING;

-- Seed correction_signals.semantics from map (read-only reference)
UPDATE correction_signals cs
SET semantics = psm.semantics
FROM pattern_semantic_map psm
WHERE cs.user_id = psm.user_id AND cs.pattern = psm.pattern AND cs.semantics IS NULL;

COMMENT ON TABLE pattern_semantic_map IS
'Growth configuration: learned semantic mappings for correction patterns. No hard-coded enum. System discovers new semantics types and validates at runtime. Confirmed patterns grow confidence.';

COMMENT ON COLUMN correction_signals.semantics IS
'Pattern semantic type (learned from pattern_semantic_map). Examples: correction, removal, alias. Not hard-coded; new types grow dynamically. NULL = unknown/unconfirmed.';
