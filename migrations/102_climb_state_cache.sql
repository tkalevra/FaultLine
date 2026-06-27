-- Migration 102: climb_state — the re_embedder classification-climb VERDICT CACHE (DB = cache)
-- Date: 2026-06-19
-- Purpose: Kill the re_embedder classification runaway. The ±6 climb
-- (climb_classification_chains) and the miss-pushback what-is classifier
-- (classify_unknown_concepts) re-attempted the SAME un-placeable concepts on EVERY poll
-- cycle, forever — continuous CLASSIFY_CHAIN / ENRICHMENT LLM calls even with no new ingest
-- (live: `apparel_item` what-is'd 9x/40s; its children re-loop the moment it's soft-deleted).
--
-- DESIGN (Alexander): do the classification work ONCE, cache the verdict, read the cache
-- BEFORE any LLM call, and re-attempt ONLY on a real additive new-info trigger — never a
-- blind every-cycle re-sweep. Hard attempt-cap + backoff as a backstop.
--
-- WHAT
-- ----
-- Per-tenant climb_state, keyed by the CONCEPT ENTITY being classified (its UUID surrogate —
-- a TYPE node, never a name; the hard line holds upstream):
--
--   entity_id       TEXT PRIMARY KEY  -- the concept entity UUID being classified
--   verdict         TEXT NOT NULL     -- 'placed' | 'unplaceable'
--   reason          TEXT              -- placed | cycle_rejected | no_parent | cap_hit | no_info_root
--   attempt_count   INT  NOT NULL     -- LLM-fired attempts so far (cap backstop)
--   last_attempt_at TIMESTAMPTZ       -- for backoff (don't retry within the window)
--   fingerprint     TEXT              -- the input fingerprint the verdict was judged against;
--                                     --   current != cached  =>  inputs changed  =>  RE-OPEN
--   created_at / updated_at
--
-- The leaf-fetch / candidate selector EXCLUDES entity_ids whose cached verdict is 'placed' or
-- 'unplaceable' AND whose fingerprint is unchanged (and, if 'unplaceable', whose attempt_count
-- is below the cap and whose backoff window has elapsed). No LLM call for a cached concept.
--
-- ADDITIVE RE-VALIDATION: an 'unplaceable' verdict is re-opened only when its fingerprint
-- changes — a new ingest touched the concept (new live hierarchy edge) OR the ontology grew a
-- candidate parent (more distinct backbone parent nodes). A concept unplaceable today MUST
-- become placeable after /expand grows the domain — that's the point.
--
-- Additive + back-compatible. public is the SEED SOURCE / TEMPLATE ONLY (never read at runtime;
-- the re_embedder binds the tenant search_path WITHOUT public). Idempotent: CREATE ... IF NOT
-- EXISTS everywhere. Mirrors the per-tenant table + fan-out shape of migrations 094/095/097.

-- ── 1. public (the template / seed source) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS public.climb_state (
    entity_id       TEXT PRIMARY KEY,
    verdict         TEXT NOT NULL,
    reason          TEXT,
    attempt_count   INT  NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMP WITH TIME ZONE,
    fingerprint     TEXT,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Selector reads by verdict + last_attempt_at (backoff window); index it.
CREATE INDEX IF NOT EXISTS idx_climb_state_verdict
    ON public.climb_state (verdict, last_attempt_at);

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.climb_state ('
            '  entity_id       TEXT PRIMARY KEY,'
            '  verdict         TEXT NOT NULL,'
            '  reason          TEXT,'
            '  attempt_count   INT  NOT NULL DEFAULT 0,'
            '  last_attempt_at TIMESTAMP WITH TIME ZONE,'
            '  fingerprint     TEXT,'
            '  created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),'
            '  updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()'
            ')',
            _schema);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_climb_state_verdict '
            'ON %I.climb_state (verdict, last_attempt_at)',
            _schema);

        RAISE NOTICE 'Migration 102: climb_state cache added to %', _schema;
    END LOOP;
END $$;
