-- Migration 168: is_single_valued on rel_types — CARDINALITY for recency-supersede-at-render
-- Date: 2026-07-12
-- Purpose: the internal design record §5 recency-picks-current.
--
-- WHAT
-- ----
-- One boolean on rel_types: does the rel hold at most ONE CURRENT value per subject?
--   is_single_valued = TRUE  → OWL-functional STATE (e.g. residence): among COEXISTING,
--                              same-subject, same-rel, UNDATED 'now' values, RECENCY picks
--                              the latest as current; older value(s) render as FORMER
--                              ("Previously, you lived in Toronto"). One current home.
--   is_single_valued = FALSE → MULTI-VALUED (e.g. speaks/likes): all coexisting current
--                              values STAY current. Recency must NOT hide any of them.
--
-- This is the metadata signal the query-side render seams key on
-- (main._recency_collapse_single_valued_state / convert_to_prose). It does NOT change ingest
-- or supersession — both values still COEXIST in the store (history kept); the flag only
-- decides whether the PRESENT view collapses to the latest at RENDER. Subject-agnostic: no
-- rel/place literal lives in code — the branch reads THIS column.
--
-- SAFE DEFAULT = FALSE (multi-valued) — the NON-DESTRUCTIVE value. An unknown / newly-grown
-- rel therefore NEVER wrongly hides a coexisting value; only rels EXPLICITLY seeded true
-- collapse. (Mirrors 096's "default to the non-destructive class".)
--
-- SEED SCOPE (deliberately MINIMAL — see the design note / needs-design report):
--   Only `lives_in` + `lives_at` are seeded true — residence is THE single-current-valued
--   example the design names ("one current home"). occupation / works_for (a person can hold
--   several jobs) and located_in (nests hierarchically: Toronto located_in Ontario AND Canada
--   both true → NOT a single-value supersede) are deliberately LEFT FALSE pending review.
--   Scalars (age/height/weight) are functional but store in entity_attributes under a UNIQUE
--   (entity_id, attribute) — only one row ever exists, so they never reach the facts coexist
--   render and need no flag here.
--
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: ADD COLUMN IF NOT EXISTS + guarded
-- UPDATEs (WHERE rel_type IN (...)). Mirrors migration 096's shape exactly.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.rel_types
    ADD COLUMN IF NOT EXISTS is_single_valued BOOLEAN NOT NULL DEFAULT false;

UPDATE public.rel_types SET is_single_valued = true
 WHERE rel_type IN ('lives_in', 'lives_at');

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
            'ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS is_single_valued BOOLEAN NOT NULL DEFAULT false',
            _schema);

        EXECUTE format(
            'UPDATE %I.rel_types SET is_single_valued = true '
            'WHERE rel_type IN (''lives_in'', ''lives_at'')',
            _schema);

        RAISE NOTICE 'Migration 168: is_single_valued added to %', _schema;
    END LOOP;
END $$;
