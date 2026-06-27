-- Migration 093: Drop the `emotion` entity_taxonomy seed; keep the `feels` rel_type
-- Date: 2026-06-15
--
-- WHY
-- ---
-- Migration 091 seeded BOTH a `feels` rel_type AND an `emotion` entity_taxonomy. The
-- `feels` rel is justified scaffold — it gives the feeling-construction's edge correct
-- routing (relational → facts, not scalar) and rendering immediately, the moment a
-- feeling is captured. KEEP IT.
--
-- The `emotion` TAXONOMY seed, however, is REDUNDANT and contradicts the self-build
-- principle. The is-a ladder self-builds via grounding: the re_embedder "what is X"
-- pushback proposes the parent, and the ladder grows in `facts`/`staged_facts` as
-- `subclass_of` edges entirely on its own. This is proven live — `melancholy
-- subclass_of emotion` and `jealous subclass_of emotion` formed without any seed; the
-- "emotion" node EMERGES from the LLM's parent proposal, it is NOT looked up from a
-- hardcoded taxonomy row. The seeded `emotion` taxonomy row is therefore just a
-- scope-grouping shortcut the self-assembling ladder makes unnecessary. Drop it and let
-- it self-build.
--
-- This migration:
--   1. DELETEs the `emotion` entity_taxonomy from public + every per-tenant schema. The
--      is-a ladder lives in `facts`/`staged_facts` as `subclass_of` edges and is NOT
--      touched — only the taxonomy grouping row goes.
--   2. Narrows `feels.tail_types` from {Concept,emotion} → {Concept} in public + every
--      per-tenant schema. Rationale: `'emotion'` is NOT a canonical entity_type — the
--      closed set the re_embedder classifier emits is Person/Animal/Organization/
--      Location/Object/Concept; feelings type as `Concept`. The `'emotion'` token in
--      tail_types only ever referenced the now-removed taxonomy. `feels` must REMAIN
--      tail_types={Concept} (relational → routes to facts, NOT scalar) so the
--      construction's edge behaves exactly as it does today.
--
-- The `feels` rel itself is otherwise UNTOUCHED (head_types, fact_class, storage_target,
-- category, natural_language all preserved).
--
-- CODE-REFERENCE AUDIT (per task): grep of src/api/main.py + src/re_embedder/embedder.py
-- (and taxonomy_overlay.py) found NO runtime lookup of the taxonomy BY NAME — every
-- `emotion` mention is a comment or an LLM-prompt prose string describing the self-build
-- behavior, never a `taxonomy_name = 'emotion'` query. Dropping the row orphans nothing.
--
-- TEMPLATE NOTE (per task item 3): migration 091 did NOT edit
-- src/provisioning/templates/user_schema.sql (verified — no `emotion` string in that
-- file). New tenants inherit the `emotion` taxonomy only via the runtime
-- _seed_entity_taxonomies copy `FROM public.entity_taxonomies`, so removing the public
-- row is sufficient to stop seeding it into future tenants. Nothing to clean in the
-- template.
--
-- Per-tenant: at runtime `search_path` EXCLUDES public, so a public-only change is a
-- silent no-op for existing tenants. Existing tenants are cleaned below via a DO loop
-- over `faultline_%` schemas (mirrors 083/091).
--
-- Idempotent: DELETE ... WHERE is a no-op if the row is already gone; the guarded UPDATE
-- of tail_types only rewrites a row that still carries `'emotion'`. Safe to re-run.
-- NOTE: after applying, FLUSH the rel_type/taxonomy overlay caches
-- (GET /internal/refresh-intent-pattern-caches?schema=...) or wait the 5s TTL.

-- ============================================================================
-- Part 1: Public schema (seed source / template)
-- ============================================================================

-- 1a. Drop the emotion taxonomy grouping row (the is-a ladder in facts is untouched).
DELETE FROM public.entity_taxonomies
WHERE taxonomy_name = 'emotion';

-- 1b. Narrow feels.tail_types {Concept,emotion} -> {Concept}. Guarded on the presence of
--     the stale 'emotion' element so it is idempotent and never clobbers a hand-tuned row.
UPDATE public.rel_types SET
    tail_types = ARRAY['Concept']::TEXT[]
WHERE rel_type = 'feels'
  AND 'emotion' = ANY(tail_types);

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
        -- ---- drop emotion taxonomy ----
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'entity_taxonomies'
        ) THEN
            EXECUTE format($del$
                DELETE FROM %I.entity_taxonomies
                WHERE taxonomy_name = 'emotion'
            $del$, _schema);
        END IF;

        -- ---- narrow feels.tail_types ----
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            EXECUTE format($upd$
                UPDATE %I.rel_types SET
                    tail_types = ARRAY['Concept']::TEXT[]
                WHERE rel_type = 'feels'
                  AND 'emotion' = ANY(tail_types)
            $upd$, _schema);
        END IF;

        RAISE NOTICE 'Migration 093: dropped emotion taxonomy + narrowed feels.tail_types in %', _schema;
    END LOOP;
END $$;
