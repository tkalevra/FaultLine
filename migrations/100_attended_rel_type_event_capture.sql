-- Migration 100: Mint the `participated_in` rel_type (event-capture seam, Option A)
-- Date: 2026-06-19 (renamed attended→participated_in 2026-06-19: see RENAME below)
--
-- RENAME (RC fix — attended→participated_in)
-- ------------------------------------------
-- This migration ORIGINALLY minted `attended` as the occurrence rel. That collided with a
-- curated alias `attends → educated_at` (migration 030, P69 "attend school", source=ontology):
-- `normalize_rel` canonicalized our occurrence `attended` → `educated_at`, so
-- "I underwent surgery on March 3, 2021" was stored as (user, educated_at, surgery) — surgery
-- filed as EDUCATION. The occurrence rel is now `participated_in` (lemma "participate"; does NOT
-- collide with the educated_at alias family — verified against rel_type_aliases + rel_types).
-- The legit `attends → educated_at` alias is LEFT INTACT (correct for school). This migration is
-- re-run-safe: it renames any `attended` rel_type rows left by a prior apply (public + every
-- tenant) to `participated_in` so no orphan occurrence rel survives.
--
-- WHY
-- ---
-- Extraction is GLiNER2 entity-pair-bound + verb-lift: an edge is only built when GLiNER2
-- surfaces TWO typeable entities for the lift. "I had a dentist visit on January 15, 2020"
-- is a LIGHT-VERB CONSTRUCTION (LVC): the eventive noun ("visit") is the semantic head and
-- "had" is an empty support verb. "dentist visit" is ONE noun phrase → no entity PAIR → no
-- edge → the parsed `event_date` then has NOTHING to attach to. The temporal recall
-- (temporal_first_last / get_first_last) and the per-edge event_date stamping are already
-- correct — they were STARVED. This is the SAME structural gap that `feels` (migration 091)
-- already solved for affective states GLiNER2 will never surface.
--
-- THE FIX (Option A — reified-occurrence edge, mirrors the `feels` seam):
-- An event = an occurrence edge (user, participated_in, <eventive-noun-phrase>) with
-- temporal_class='event'. The deterministic `_detect_event_states` seam (src/api/main.py,
-- spaCy `analyze_event` — grammatical light-verb + eventive-noun detection, NO event-noun
-- word-list, NO LLM, NO GLiNER2) constructs the pair; the eventive noun grounds into an
-- `event` place via the EXISTING re_embedder "what is X" pushback (exactly as anxious→emotion);
-- the DATE rides the existing per-edge event_date gate (no change there).
-- Spec: DEV/DESIGN-feeling-and-temporal-capture.md (events = reified occurrences).
-- Precedent: `met` is already temporal_class='event' (migration 096).
--
-- THE HARD LINE: event-TYPE is a PLACE (a node in the L4 hierarchy), the DATE is a SCALAR
-- leaf — NEVER conflated. This rel only routes the TYPE edge to facts; event_date is the leaf.
--
-- METADATA RATIONALE (participated_in)
-- ------------------------------------
--   * head_types = {Person}        — a person (the participant) attends/has an occurrence.
--   * tail_types = {Concept}        — the eventive noun is a Concept (the re_embedder's closed
--     classifier set is Person/Animal/Organization/Location/Object/Concept). NOT a SCALAR, so it
--     routes to facts/staged (relational), NEVER entity_attributes. We DELIBERATELY do NOT put a
--     non-canonical 'event' token in tail_types — that is the exact mistake migration 093 fixed
--     for `feels` ({Concept,emotion} -> {Concept}); the `event` place EMERGES from the self-built
--     is-a ladder (dentist_visit → appointment → event via subclass_of), it is NOT a tail type.
--   * temporal_class = 'event'      — distinct DATABLE occurrences that COEXIST (vs 'state'/
--     'immutable'); this is what lets multiple dated visits live side-by-side and feeds the
--     temporal_first_last recall. Mirrors `met` (migration 096).
--   * is_hierarchy_rel = false      — `participated_in` is an ASSOCIATIVE participation edge
--     (participant→occurrence), not classification/composition. The event is-a ladder
--     (dentist_visit→appointment→event) is carried by subclass_of, kept orthogonal (graph ≠
--     hierarchy).
--   * fact_class = 'C'              — an occurrence is TRANSIENT/tentative by default (Class C:
--     staged). user_stated provenance floors it to Class B at ingest (assign_class), which is
--     correct — a one-off event stays tentative, a recurring kind promotes (>=3). Mirrors `feels`.
--   * is_symmetric = false, inverse = NULL — directional, no inverse.
--   * storage_target = 'facts'      — relational edge (Concept object), staged→facts.
--   * category = 'event'            — net-new free-text category (column is plain TEXT, no CHECK;
--     cf. 091 `affective`, 083 `behavioral`).
--   * source = 'builtin'            — FaultLine domain rel, stable.
--   * natural_language / _2p        — recall render templates (3p + 2p), cf. 081/083/091.
--
-- TAXONOMY: DELIBERATELY NOT SEEDED (the 093 lesson)
-- --------------------------------------------------
-- Migration 091 seeded an `emotion` taxonomy alongside `feels`; migration 093 then DROPPED it
-- as redundant because the is-a ladder SELF-BUILDS via the re_embedder "what is X" pushback
-- (melancholy/jealous subclass_of emotion formed with no seed). The same self-build principle
-- applies to events: dentist_visit → appointment → event grows on its own under subclass_of.
-- Per CLAUDE.md ("NO seeded member words — the ladder self-builds via what-is") and the explicit
-- 093 correction, we mint ONLY the `participated_in` rel and let the `event` place emerge. No
-- entity_taxonomies row is seeded.
--
-- Per-tenant: runtime search_path EXCLUDES public, so the public row is template/seed only.
-- New tenants inherit `participated_in` via the template's `INSERT INTO rel_types SELECT * FROM
-- public.rel_types` (no edit to user_schema.sql needed — the temporal_class column + CHECK are
-- already in the template). EXISTING tenants are backfilled by the DO loop below (mirrors 091).
-- Idempotent: rename-if-present + ON CONFLICT DO NOTHING + guarded UPDATE. Safe to re-run.
--
-- NOTE (testing): after applying, FLUSH the rel_type/taxonomy overlay caches
-- (GET /internal/refresh-intent-pattern-caches?schema=...) or wait the 5s TTL. For a clean
-- live test ALSO flush Redis (`docker exec faultline-redis redis-cli FLUSHALL`) so the
-- idempotency cache does not swallow a re-ingest of the same test sentence, and restart the
-- faultline-mcp container to clear the in-memory _provisioned_users set.

-- ============================================================================
-- Part 0: Rename any prior-apply `attended` occurrence rel → `participated_in`
--          (public seed/template). Re-run-safe: only fires if `attended` exists
--          AND was NOT already promoted to `participated_in`.
-- ============================================================================

-- Rename only the occurrence rel we minted (category='event'), never some other 'attended'.
UPDATE public.rel_types
   SET rel_type = 'participated_in',
       label    = 'Participated in'
 WHERE rel_type = 'attended'
   AND category = 'event'
   AND NOT EXISTS (SELECT 1 FROM public.rel_types WHERE rel_type = 'participated_in');

-- ============================================================================
-- Part 1: Public schema (seed source / template)
-- ============================================================================

-- 1a. participated_in rel_type skeleton
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior)
VALUES
    ('participated_in', 'Participated in', NULL, false, 1.0, 'builtin', 'supersede')
ON CONFLICT (rel_type) DO NOTHING;

-- 1b. participated_in metadata (guarded — only fills the freshly-inserted skeleton)
UPDATE public.rel_types SET
    head_types          = ARRAY['Person']::TEXT[],
    tail_types          = ARRAY['Concept']::TEXT[],
    is_symmetric        = false,
    inverse_rel_type    = NULL,
    is_hierarchy_rel    = false,
    fact_class          = 'C',
    storage_target      = 'facts',
    category            = 'event',
    temporal_class      = 'event',
    natural_language    = 'X participated in Y',
    natural_language_2p = 'You participated in Y'
WHERE rel_type = 'participated_in'
  AND (head_types IS NULL OR head_types = '{}');

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
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            -- ---- Part 0 (tenant): rename a prior-apply `attended` occurrence rel ----
            -- Rename both the rel_types row AND any facts rows that already used `attended`
            -- (a prior apply may have stored occurrence edges under `attended`) so nothing
            -- is orphaned. Guarded: only the event-category rel, only if not already renamed.
            EXECUTE format($ren$
                UPDATE %I.rel_types
                   SET rel_type = 'participated_in',
                       label    = 'Participated in'
                 WHERE rel_type = 'attended'
                   AND category = 'event'
                   AND NOT EXISTS (
                        SELECT 1 FROM %I.rel_types WHERE rel_type = 'participated_in')
            $ren$, _schema, _schema);

            -- Migrate any occurrence facts written under a bare `attended` rel_type.
            -- NOTE: the bug being fixed is that the curated alias canonicalized `attended`→
            -- `educated_at` BEFORE storage, so the MISFILED prior-apply rows are actually
            -- `(user, educated_at, surgery)`, NOT `(user, attended, surgery)`. We deliberately do
            -- NOT blind-rename `educated_at` rows here — that would clobber legitimate education
            -- facts and we cannot deterministically tell "surgery filed as education" from a real
            -- school fact. Those misfiled rows are prior-apply pollution on a TEST tenant; the
            -- clean re-test (wipe + re-ingest through the now-correct `participated_in` path) is
            -- the recovery. This rename only catches any rare bare-`attended` rows that slipped
            -- past the alias (defensive, idempotent).
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = _schema AND table_name = 'facts'
            ) THEN
                EXECUTE format($mf$
                    UPDATE %I.facts SET rel_type = 'participated_in'
                     WHERE rel_type = 'attended'
                $mf$, _schema);
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = _schema AND table_name = 'staged_facts'
            ) THEN
                EXECUTE format($ms$
                    UPDATE %I.staged_facts SET rel_type = 'participated_in'
                     WHERE rel_type = 'attended'
                $ms$, _schema);
            END IF;

            -- ---- participated_in rel_type ----
            EXECUTE format($ins$
                INSERT INTO %I.rel_types
                    (rel_type, label, wikidata_pid, engine_generated, confidence,
                     source, correction_behavior)
                VALUES
                    ('participated_in', 'Participated in', NULL, false, 1.0,
                     'builtin', 'supersede')
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);

            EXECUTE format($upd$
                UPDATE %I.rel_types SET
                    head_types          = ARRAY['Person']::TEXT[],
                    tail_types          = ARRAY['Concept']::TEXT[],
                    is_symmetric        = false,
                    inverse_rel_type    = NULL,
                    is_hierarchy_rel    = false,
                    fact_class          = 'C',
                    storage_target      = 'facts',
                    category            = 'event',
                    temporal_class      = 'event',
                    natural_language    = 'X participated in Y',
                    natural_language_2p = 'You participated in Y'
                WHERE rel_type = 'participated_in'
                  AND (head_types IS NULL OR head_types = '{}')
            $upd$, _schema);
        END IF;

        RAISE NOTICE 'Migration 100: minted participated_in (event-capture) into %', _schema;
    END LOOP;
END $$;
