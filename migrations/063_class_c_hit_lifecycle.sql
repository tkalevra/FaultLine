-- Migration 063: Class C hit-lifecycle on staged_facts
--
-- Class C = rough short-term memory. ANYTHING that misses structured extraction on
-- ingest (classified or not) is stored as Class C. Promotion to Class B is earned by
-- genuine query-scoped relevance hits, not by bulk full-scope returns.
--
-- ───────────────────────────────────────────────────────────────────────────────
-- STATE MACHINE (per Class C staged_facts row: hit_count h, expires_at e)
--
--   CREATE (ingest miss, classified or not):
--       h = 1,  e = now() + 30d,  vector stored in Qdrant
--
--   QUERY-SCOPED HIT (genuine relevance match — NOT a bulk full-scope return):
--       h = h + 1
--       e = now() + 30d                 -- any hit resets the 30-day clock
--       if h >= 3:  PROMOTE -> Class B   (re_embedder, classify-if-needed)
--
--   IDLE 30 DAYS (e elapsed with no hit in the window):
--       h = h - 1
--       e = now() + 30d                 -- decrement buys another 30-day window
--       if h <= 0:  DROP (delete)
--
--   BULK FULL-SCOPE RETURN (not a scoped match):
--       no-op  -- does NOT increment (anti-noise guarantee)
--
-- Net: hits push up (+1, reset clock), idle windows push down (-1 per 30d).
-- 3 = promote, 0 = drop. A never-referenced Class C row dies in ~30 days (1 -> 0).
--
-- OWNERSHIP:
--   • Query path (src/api/main.py)        : CREATE (rough store) + scoped-HIT increment.
--   • re_embedder (embedder.py)           : idle decay + drop + promote-at-3.
--
-- hit_count is DISTINCT from confirmed_count:
--   • confirmed_count = ingest-side confirmation counter for Class B promotion.
--   • hit_count       = query-side relevance-hit counter for Class C promotion.
-- ───────────────────────────────────────────────────────────────────────────────

-- Query-hit counter for Class C rough memory. Defaults to 1 (CREATE state h=1).
ALTER TABLE staged_facts
    ADD COLUMN IF NOT EXISTS hit_count INT NOT NULL DEFAULT 1;

-- Rough/unclassified Class C memory must be storable without a rel_type.
-- Drop NOT NULL on rel_type if it is currently enforced (idempotent guard).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'staged_facts'
          AND column_name = 'rel_type'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE staged_facts ALTER COLUMN rel_type DROP NOT NULL;
    END IF;
END $$;

-- Index to let the re_embedder efficiently scan Class C rows by lifecycle state.
CREATE INDEX IF NOT EXISTS idx_staged_facts_class_c_lifecycle
    ON staged_facts (fact_class, expires_at, hit_count)
    WHERE fact_class = 'C' AND promoted_at IS NULL;
