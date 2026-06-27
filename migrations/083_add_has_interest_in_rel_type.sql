-- Migration 083: Mint the `has_interest_in` rel_type
-- Date: 2026-06-12
--
-- WHY
-- ---
-- No interest-semantic relation exists in the seed ontology. `has_interest_in`,
-- `interested_in`, and `studies` are all absent, and the existing `likes`/`prefers`
-- carry SENTIMENT ("I enjoy X") rather than the topical "I am actively exploring /
-- learning about X" semantic we need. The topic-self-anchoring design currently
-- fabricates a `user related_to {topic}` edge as a stand-in; `related_to` is a loose
-- symmetric link that pollutes traversal. This migration mints a clean, first-class
-- `has_interest_in` so:
--   1. the topic-self-anchoring design can emit an HONEST user→topic edge instead of
--      the `related_to` hack, and
--   2. the default-scope greeting feature has a real rel to project a user's current
--      interests from.
--
-- METADATA RATIONALE
-- ------------------
--   * is_hierarchy_rel = false  — interest is an ASSOCIATIVE edge, NOT classification or
--     composition. Keeping it off the hierarchy systems isolates it from the family /
--     taxonomy hierarchy traversal (graph ≠ hierarchy; do not conflate).
--   * fact_class = 'B'  — interest is LLM-/action-derived ("you've been reading about Y"),
--     not user-stated identity. Class B → staged, promoted on confirmation (>=3); never
--     Class A (Class A is user-stated identity only).
--   * head_types = {Person}      — only people hold interests.
--   * tail_types = {ANY, Concept} — the object is a topic/concept (or any entity surrogate).
--   * is_symmetric = false, inverse = NULL — interest is directional and has no inverse.
--   * storage_target = 'facts'   — relational edge (UUID object), staged→facts like `likes`.
--   * category = 'behavioral'    — net-new free-text category value (the `category` column
--     is plain TEXT with NO CHECK constraint — migration 013_rel_type_category.sql adds it
--     as `ADD COLUMN ... category TEXT` with no constraint — so a novel value is accepted).
--   * source = 'builtin'         — FaultLine domain-specific, stable, not Wikidata-aligned
--     (matches the `source` CHECK: wikidata|builtin|engine|user). Mirrors `likes`/`prefers`.
--   * natural_language / _2p     — recall render templates (3p + 2p), so prose stays clean
--     (precedent: migrations 031/068/081).
--
-- SHAPE is mirrored from `likes` (head {Person,Animal}, tail {ANY}, non-hierarchy, Class B),
-- narrowed to head {Person} and tail {ANY, Concept}.
--
-- Per-tenant: at runtime `search_path` EXCLUDES public, so a public-only change is a silent
-- no-op for existing tenants. New tenants get the row automatically via the template's
-- `INSERT INTO rel_types SELECT * FROM public.rel_types` (user_schema.sql). EXISTING tenants
-- are backfilled below via a DO loop over `faultline_%` schemas, mirroring migration 079/081.
--
-- Idempotent: ON CONFLICT DO NOTHING on insert; guarded UPDATE only sets metadata when the
-- row is still missing it; tenant loop is INSERT ... ON CONFLICT DO NOTHING. Safe to re-run.

-- ============================================================================
-- Part 1: Public schema (seed source / template)
-- ============================================================================

-- 1a. Insert the row (skeleton). Two-step seed convention (cf. 005 + 030).
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior)
VALUES
    ('has_interest_in', 'Has Interest In', NULL, false, 1.0, 'builtin', 'supersede')
ON CONFLICT (rel_type) DO NOTHING;

-- 1b. Populate metadata (guarded — only fills the freshly-inserted skeleton row; the
--     head_types guard makes this idempotent and prevents clobbering hand-tuned values).
UPDATE public.rel_types SET
    head_types          = ARRAY['Person']::TEXT[],
    tail_types          = ARRAY['ANY', 'Concept']::TEXT[],
    is_symmetric        = false,
    inverse_rel_type    = NULL,
    is_hierarchy_rel    = false,
    fact_class          = 'B',
    storage_target      = 'facts',
    category            = 'behavioral',
    natural_language    = 'X is interested in Y',
    natural_language_2p = 'You are interested in Y'
WHERE rel_type = 'has_interest_in'
  AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- New tenants inherit the row from the template's SELECT * FROM public.rel_types.
-- Existing tenants need it pushed in. The overlay (rel_type_overlay.py) then surfaces
-- it at runtime — no code change required.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            -- 2a. Insert skeleton row (ON CONFLICT DO NOTHING — never clobber).
            EXECUTE format($ins$
                INSERT INTO %I.rel_types
                    (rel_type, label, wikidata_pid, engine_generated, confidence,
                     source, correction_behavior)
                VALUES
                    ('has_interest_in', 'Has Interest In', NULL, false, 1.0,
                     'builtin', 'supersede')
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);

            -- 2b. Populate metadata (guarded on missing head_types — idempotent).
            EXECUTE format($upd$
                UPDATE %I.rel_types SET
                    head_types          = ARRAY['Person']::TEXT[],
                    tail_types          = ARRAY['ANY', 'Concept']::TEXT[],
                    is_symmetric        = false,
                    inverse_rel_type    = NULL,
                    is_hierarchy_rel    = false,
                    fact_class          = 'B',
                    storage_target      = 'facts',
                    category            = 'behavioral',
                    natural_language    = 'X is interested in Y',
                    natural_language_2p = 'You are interested in Y'
                WHERE rel_type = 'has_interest_in'
                  AND (head_types IS NULL OR head_types = '{}')
            $upd$, _schema);

            RAISE NOTICE 'Migration 083: minted has_interest_in into %', _schema;
        END IF;
    END LOOP;
END $$;
