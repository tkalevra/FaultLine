-- Migration 091: Mint the `feels` rel_type + the `emotion` entity_taxonomy
-- Date: 2026-06-15
--
-- WHY
-- ---
-- Extraction is GLiNER2 entity-pair-bound: an edge is only built when GLiNER2 surfaces
-- two typeable entities for verb-lift. A FEELING ("worried", "anxious", "thinking") is
-- NOT an entity GLiNER2 will ever surface (and per Pitfall 11 it MUST NOT be), so feeling
-- statements ("I am worried", "I feel anxious") are dropped entirely — or, worse, caught
-- by the loose name regex and mis-filed as `also_known_as`. Feelings are real, capturable
-- facts (the user's affective state). This mints the seam GLiNER2 deliberately doesn't
-- cover: a `feels` relation + an `emotion` taxonomy to ground feelings into.
-- Spec: DEV/DESIGN-feeling-and-temporal-capture.md (research-grounded: Emotion Frame
-- Ontology — experiencer→emotion→trigger; intensity-ordered is-a ladder).
--
-- METADATA RATIONALE (feels)
-- --------------------------
--   * head_types = {Person}        — a person (the experiencer) feels.
--   * tail_types = {Concept,emotion}— the object is an affective concept (grounded into
--     the `emotion` taxonomy by the re_embedder "what is X" pushback; NOT a SCALAR, so it
--     routes to facts/staged, never entity_attributes).
--   * is_hierarchy_rel = false      — `feels` is an ASSOCIATIVE edge (experiencer→emotion),
--     not classification/composition. The emotion is-a ladder (worried→anxiety→fear→emotion)
--     is carried by instance_of/subclass_of, kept orthogonal (graph ≠ hierarchy).
--   * fact_class = 'C'              — a feeling is TRANSIENT/tentative (Class C: staged,
--     decays). user_stated provenance still floors it to Class B at ingest (assign_class),
--     which is correct — a one-off feeling stays tentative, a recurring one promotes (>=3).
--   * is_symmetric = false, inverse = NULL — directional, no inverse.
--   * storage_target = 'facts'      — relational edge (Concept object), staged→facts.
--   * category = 'affective'        — net-new free-text category (column is plain TEXT, no
--     CHECK; cf. 083 `behavioral`).
--   * source = 'builtin'            — FaultLine domain rel, stable, not Wikidata-aligned.
--   * natural_language / _2p        — recall render templates (3p + 2p), cf. 081/083.
--
-- METADATA RATIONALE (emotion taxonomy)
-- -------------------------------------
--   * member_entity_types = {Concept, emotion} — feeling concepts type as Concept (the
--     re_embedder classifier emits one of 6 canonical types) or the net-new `emotion`.
--   * rel_types_defining_group = {feels}        — an entity reached via `feels` is in the
--     emotion group (membership-by-rel), so a "how do I feel" scope can resolve.
--   * is_hierarchical = true, parent_rel_type = subclass_of — enables the intensity-ordered
--     is-a ladder (Fear → Anxiety → Dread …) to grow under it, gated by ontology_evaluations.
--
-- Per-tenant: runtime search_path EXCLUDES public, so the public rows are template/seed only.
-- New tenants inherit both via the template's `INSERT ... SELECT * FROM public.*` (rel_types
-- in user_schema.sql; entity_taxonomies via _seed_entity_taxonomies). EXISTING tenants are
-- backfilled by the DO loops below (mirrors 083). Idempotent: ON CONFLICT DO NOTHING +
-- guarded UPDATE. Safe to re-run. NOTE: after applying, FLUSH the rel_type/taxonomy overlay
-- caches (GET /internal/refresh-intent-pattern-caches?schema=...) or wait the 5s TTL.

-- ============================================================================
-- Part 1: Public schema (seed source / template)
-- ============================================================================

-- 1a. feels rel_type skeleton
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior)
VALUES
    ('feels', 'Feels', NULL, false, 1.0, 'builtin', 'supersede')
ON CONFLICT (rel_type) DO NOTHING;

-- 1b. feels metadata (guarded — only fills the freshly-inserted skeleton)
UPDATE public.rel_types SET
    head_types          = ARRAY['Person']::TEXT[],
    tail_types          = ARRAY['Concept', 'emotion']::TEXT[],
    is_symmetric        = false,
    inverse_rel_type    = NULL,
    is_hierarchy_rel    = false,
    fact_class          = 'C',
    storage_target      = 'facts',
    category            = 'affective',
    natural_language    = 'X feels Y',
    natural_language_2p = 'You feel Y'
WHERE rel_type = 'feels'
  AND (head_types IS NULL OR head_types = '{}');

-- 1c. emotion taxonomy
INSERT INTO public.entity_taxonomies
    (taxonomy_name, description, member_entity_types, rel_types_defining_group,
     has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type, source)
VALUES
    ('emotion',
     'Affective states / feelings a person experiences (the object of the feels relation). Grows an intensity-ordered is-a ladder under basic emotions via subclass_of.',
     ARRAY['Concept', 'emotion']::TEXT[],
     ARRAY['feels']::TEXT[],
     false,
     ARRAY[]::TEXT[],
     true,
     'subclass_of',
     'seeded')
ON CONFLICT (taxonomy_name) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- ---- feels rel_type ----
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            EXECUTE format($ins$
                INSERT INTO %I.rel_types
                    (rel_type, label, wikidata_pid, engine_generated, confidence,
                     source, correction_behavior)
                VALUES
                    ('feels', 'Feels', NULL, false, 1.0, 'builtin', 'supersede')
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);

            EXECUTE format($upd$
                UPDATE %I.rel_types SET
                    head_types          = ARRAY['Person']::TEXT[],
                    tail_types          = ARRAY['Concept', 'emotion']::TEXT[],
                    is_symmetric        = false,
                    inverse_rel_type    = NULL,
                    is_hierarchy_rel    = false,
                    fact_class          = 'C',
                    storage_target      = 'facts',
                    category            = 'affective',
                    natural_language    = 'X feels Y',
                    natural_language_2p = 'You feel Y'
                WHERE rel_type = 'feels'
                  AND (head_types IS NULL OR head_types = '{}')
            $upd$, _schema);
        END IF;

        -- ---- emotion taxonomy ----
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'entity_taxonomies'
        ) THEN
            EXECUTE format($tax$
                INSERT INTO %I.entity_taxonomies
                    (taxonomy_name, description, member_entity_types,
                     rel_types_defining_group, has_transitivity, transitive_rel_types,
                     is_hierarchical, parent_rel_type, source)
                VALUES
                    ('emotion',
                     'Affective states / feelings a person experiences (the object of the feels relation). Grows an intensity-ordered is-a ladder under basic emotions via subclass_of.',
                     ARRAY['Concept', 'emotion']::TEXT[],
                     ARRAY['feels']::TEXT[],
                     false,
                     ARRAY[]::TEXT[],
                     true,
                     'subclass_of',
                     'seeded')
                ON CONFLICT (taxonomy_name) DO NOTHING
            $tax$, _schema);
        END IF;

        RAISE NOTICE 'Migration 091: minted feels + emotion taxonomy into %', _schema;
    END LOOP;
END $$;
