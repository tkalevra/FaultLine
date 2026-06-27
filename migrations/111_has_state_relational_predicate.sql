-- Migration 111: Re-mint `has_state` as a RELATIONAL state predicate (supersedes 110)
-- Date: 2026-06-21
--
-- WHY
-- ---
-- Migration 110 minted `has_state` as a SCALAR predicate (tail_types={SCALAR},
-- storage_target='entity_attributes'): an intransitive state ("My car's GPS broke last week")
-- was stored as a freeform STRING memory leaf, never typed, never reusable. The owner REJECTED
-- that as "noise" (DEV/bugs/strength-passing-recursion/DESIGN-state-typing.md). The replacement
-- decision: a STATE ("broken"/"break") is the structural TWIN of a FEELING ("worried") — an
-- associative edge to a TYPED, SELF-BUILDING hierarchy NODE, never a scalar leaf. A GPS, a
-- server and a leg all point to the SAME `break` node, and the is-a ladder over it self-builds
-- via the async re_embedder grounder (exactly as `worried subclass_of emotion` self-builds).
--
-- This makes `has_state` behave like `feels` (migration 091), NOT like `has_ip`. It mirrors the
-- 093 pattern of correcting a prior migration's metadata IN PLACE in a forward migration: 093
-- narrowed 091's `feels` row; 111 re-mints 110's `has_state` row from SCALAR → RELATIONAL.
--
-- SELF-BUILD (mirror 093): NO `state` taxonomy is seeded. The is-a ladder self-builds from the
-- async grounder's parent proposals (`break subclass_of <impaired/non_functional> → state`),
-- entirely in facts/staged_facts, exactly as the `emotion` node EMERGED for feelings after 093
-- dropped its taxonomy seed. Zero new seed; the existing grounder + convergence-by-identity do
-- the work. The FIRST sighting of a genuinely-new state takes ONE async backstop call to ladder
-- (then deterministic forever via the already-laddered gate).
--
-- METADATA RATIONALE (has_state — RELATIONAL, mirrors `feels`)
-- -----------------------------------------------------------
--   * head_types = {ANY}            — ANY thing can be in a state (a gps, a server, a car, a
--     person). Subject-agnostic; never a domain type list.
--   * tail_types = {Concept}        — THE load-bearing flip: a relational, TYPEABLE Concept
--     object (the state node types as the canonical `Concept`, like feelings) → routed to
--     facts/staged (NOT entity_attributes), resolved to a UUID, grounded into a self-building
--     state hierarchy. NOT {SCALAR} (that was 110's noise).
--   * is_hierarchy_rel = false      — `has_state` is an ASSOCIATIVE edge (thing→state), not
--     classification/composition. The state is-a ladder (break→impaired→state) is carried by
--     subclass_of/instance_of, kept orthogonal (graph != hierarchy) — same as `feels`.
--   * storage_target = 'facts'      — relational edge (Concept object), staged→facts.
--   * fact_class = 'B'              — the engine typed it; user_stated provenance floors to B
--     (assign_class), and a directly-asserted state reaches A only via the correction path —
--     EXACTLY as `feels` behaves. Not A by default (would over-assert every narrated state).
--   * temporal_class = 'event'      — a dated occurrence/state (drives the temporal lifecycle).
--   * is_symmetric = false, inverse = NULL — directional, no inverse.
--   * scalar_datatype = NULL        — relational object, no scalar datatype (clears 110's value).
--   * category = 'state'            — net-new free-text category (column is plain TEXT, no CHECK;
--     cf. 091 `affective`, 083 `behavioral`).
--   * correction_behavior = 'supersede' — a corrected state replaces the prior (cf. `feels`).
--   * source = 'builtin'            — FaultLine domain rel, stable, not Wikidata-aligned.
--   * natural_language / _2p        — recall render templates (3p + 2p), kept from 110.
--
-- WHY ON CONFLICT DO UPDATE (NOT DO NOTHING): migration 110 ALREADY inserted+filled the
-- `has_state` row with SCALAR metadata. A DO-NOTHING insert + a guarded `WHERE head_types IS
-- NULL` update (the 091/110 pattern) would BOTH no-op on the existing row and leave it SCALAR.
-- 111 must FORCE the corrected metadata, so the public insert uses ON CONFLICT DO UPDATE and the
-- per-tenant loop runs an UNGUARDED UPDATE that always overrides. (Migration 110 stays in
-- history; 111 supersedes its metadata forward.)
--
-- Per-tenant: runtime search_path EXCLUDES public, so the public row is template/seed only. New
-- tenants inherit it via the template's `INSERT ... SELECT * FROM public.rel_types`
-- (user_schema.sql). EXISTING tenants are corrected by the DO loop below. Idempotent: re-running
-- writes the same RELATIONAL metadata. NOTE: after applying, FLUSH the rel_type overlay caches
-- (GET /internal/refresh-intent-pattern-caches?schema=...) or wait the 5s TTL.

-- ============================================================================
-- Part 1: Public schema (seed source / template)
-- ============================================================================

-- 1a. Ensure the has_state skeleton exists (no-op if 110 already created it). DO UPDATE forces
--     the corrected correction_behavior even when the row pre-exists.
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior)
VALUES
    ('has_state', 'Has State', NULL, false, 1.0, 'builtin', 'supersede')
ON CONFLICT (rel_type) DO UPDATE SET
    correction_behavior = EXCLUDED.correction_behavior;

-- 1b. has_state metadata — RELATIONAL (UNGUARDED: overrides 110's SCALAR row).
UPDATE public.rel_types SET
    head_types          = ARRAY['ANY']::TEXT[],
    tail_types          = ARRAY['Concept']::TEXT[],
    is_symmetric        = false,
    inverse_rel_type    = NULL,
    is_hierarchy_rel    = false,
    fact_class          = 'B',
    storage_target      = 'facts',
    category            = 'state',
    temporal_class      = 'event',
    scalar_datatype     = NULL,
    natural_language    = 'X is in state Y',
    natural_language_2p = 'You are in state Y'
WHERE rel_type = 'has_state';

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
            EXECUTE format($ins$
                INSERT INTO %I.rel_types
                    (rel_type, label, wikidata_pid, engine_generated, confidence,
                     source, correction_behavior)
                VALUES
                    ('has_state', 'Has State', NULL, false, 1.0, 'builtin', 'supersede')
                ON CONFLICT (rel_type) DO UPDATE SET
                    correction_behavior = EXCLUDED.correction_behavior
            $ins$, _schema);

            EXECUTE format($upd$
                UPDATE %I.rel_types SET
                    head_types          = ARRAY['ANY']::TEXT[],
                    tail_types          = ARRAY['Concept']::TEXT[],
                    is_symmetric        = false,
                    inverse_rel_type    = NULL,
                    is_hierarchy_rel    = false,
                    fact_class          = 'B',
                    storage_target      = 'facts',
                    category            = 'state',
                    temporal_class      = 'event',
                    scalar_datatype     = NULL,
                    natural_language    = 'X is in state Y',
                    natural_language_2p = 'You are in state Y'
                WHERE rel_type = 'has_state'
            $upd$, _schema);
        END IF;

        RAISE NOTICE 'Migration 111: re-minted has_state as RELATIONAL (supersedes 110) in %', _schema;
    END LOOP;
END $$;
