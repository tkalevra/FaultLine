-- Migration: Seed negation_patterns with linguistic baseline
-- Date: 2026-05-27
-- Purpose: Bootstrap global negation patterns (apply to all users, all languages)
--          These are language-level features, not domain-specific
--          System will learn user-specific overrides on top of this baseline

-- Global user_id for non-user-specific patterns
-- Using all zeros as the "global" user marker
INSERT INTO negation_patterns (user_id, pattern_text, negation_type, learned_from, confidence, created_at)
VALUES
  ('00000000-0000-0000-0000-000000000000'::uuid, 'is not', 'retraction', 'linguistic_bootstrap', 0.95, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'is not a', 'retraction', 'linguistic_bootstrap', 0.95, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'is not an', 'retraction', 'linguistic_bootstrap', 0.95, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'no longer', 'retraction', 'linguistic_bootstrap', 0.95, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'not anymore', 'retraction', 'linguistic_bootstrap', 0.95, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'never', 'retraction', 'linguistic_bootstrap', 0.90, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'forget', 'retraction', 'linguistic_bootstrap', 0.92, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'delete', 'retraction', 'linguistic_bootstrap', 0.92, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'remove', 'retraction', 'linguistic_bootstrap', 0.90, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'erase', 'retraction', 'linguistic_bootstrap', 0.90, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'wrong', 'correction', 'linguistic_bootstrap', 0.88, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'actually', 'correction', 'linguistic_bootstrap', 0.82, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'i meant', 'correction', 'linguistic_bootstrap', 0.85, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'changed my mind', 'correction', 'linguistic_bootstrap', 0.90, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'mistake', 'correction', 'linguistic_bootstrap', 0.88, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'incorrect', 'correction', 'linguistic_bootstrap', 0.88, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'typo', 'correction', 'linguistic_bootstrap', 0.80, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'not true', 'retraction', 'linguistic_bootstrap', 0.92, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'that is wrong', 'correction', 'linguistic_bootstrap', 0.88, now()),
  ('00000000-0000-0000-0000-000000000000'::uuid, 'not the case', 'retraction', 'linguistic_bootstrap', 0.90, now())
ON CONFLICT (user_id, pattern_text, negation_type) DO NOTHING;

-- Index for fast global pattern lookup (complement existing user-specific index)
CREATE INDEX IF NOT EXISTS idx_negation_global_patterns
ON negation_patterns (pattern_text, negation_type)
WHERE user_id = '00000000-0000-0000-0000-000000000000'::uuid;
