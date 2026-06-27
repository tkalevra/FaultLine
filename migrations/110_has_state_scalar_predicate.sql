-- Migration 110: Mint the `has_state` SCALAR state predicate
-- Date: 2026-06-21
--
-- WHY
-- ---
-- An intransitive "something happened to a thing" clause ("My car's GPS broke last week",
-- "the server is down", "my car got towed") names a THING and a STATE that befell it. The
-- sentence-pipeline deriver detects this STRUCTURALLY (a content verb with a subject and no
-- object — NOT a verb list) in `_chain_intransitive` (src/extraction/linguistics.py). Before
-- this seam, that chain emitted `(subject, <verb-lemma>, <verb-surface-as-object>)` — which
-- made the verb surface ("broke") a registered ENTITY, queued it for grounding, and the
-- context-free grounder mis-sensed it ("broke"=bankrupt) into a FINANCIAL L4 hierarchy.
-- Two symptoms, one root cause. (DEV/bugs/strength-passing-recursion/SPEC.md §10.4.)
--
-- THE OWNER'S DECISION (SPEC §10.4): the THING is grounded; the STATE is the DATED MEMORY
-- attached to it — NOT an entity, NOT resolved to a UUID, NOT grounded into L4 (there is no
-- meaningful hierarchy for "broke"). It is user-truth memory content stuck to the thing,
-- with the event_date. FaultLine already has exactly that discipline: the SCALAR object path
-- (tail_types={SCALAR}) stores a STRING value in entity_attributes that is NEVER resolved to
-- a UUID and NEVER L4-placed/vector-indexed. The state rides that SAME path.
--
-- This migration mints the single canonical, subject-agnostic predicate the deriver routes
-- the state through (referenced once in code as `_STATE_REL`, mirroring the
-- `_CLASSIFICATION_RETYPE_REL = "instance_of"` pattern — a named pointer at an
-- ontology-defined predicate, not a hardcoded verb dispatch).
--
-- METADATA RATIONALE (has_state)
-- ------------------------------
--   * head_types = {ANY}            — ANY thing can be in a state (a gps, a server, a car, a
--     person). Subject-agnostic; never a domain type list.
--   * tail_types = {SCALAR}         — THE load-bearing choice: SCALAR routes the object to the
--     entity_attributes string path → never resolved to a UUID, never registered, never
--     grounded into L4, and SKIPPED by the harvest backbone-attach loop (_is_scalar_rel_type).
--   * scalar_datatype = 'string'    — the state value is freeform text ("broke"/"down"/
--     "got towed"); shape-free validation (no strict format), no coercion mangling.
--   * is_hierarchy_rel = false      — a state is an ASSOCIATIVE leaf memory, not classification.
--   * is_symmetric = false, inverse = NULL — directional, no inverse.
--   * storage_target = 'entity_attributes' — the scalar leaf-memory home (the event_date rides
--     into value_date at ingest so the dated memory is complete).
--   * fact_class = 'B'              — an inferred state on a thing; user_stated provenance still
--     floors it to B at ingest (assign_class). Not A (not a stable identity attribute).
--   * temporal_class = 'event'      — a dated occurrence/state (drives the temporal lifecycle),
--     not an immutable identity nor an open-ended state-of-being.
--   * category = 'state'            — net-new free-text category (column is plain TEXT, no
--     CHECK; cf. 091 `affective`, 083 `behavioral`).
--   * source = 'builtin'            — FaultLine domain rel, stable, not Wikidata-aligned.
--   * natural_language / _2p        — recall render templates (3p + 2p), cf. 081/091.
--
-- Per-tenant: runtime search_path EXCLUDES public, so the public row is template/seed only.
-- New tenants inherit it via the template's `INSERT ... SELECT * FROM public.rel_types`
-- (user_schema.sql). EXISTING tenants are backfilled by the DO loop below (mirrors 091).
-- Idempotent: ON CONFLICT DO NOTHING + guarded UPDATE. Safe to re-run. NOTE: after applying,
-- FLUSH the rel_type overlay caches (GET /internal/refresh-intent-pattern-caches?schema=...)
-- or wait the 5s TTL.

-- ============================================================================
-- Part 1: Public schema (seed source / template)
-- ============================================================================

-- 1a. has_state rel_type skeleton
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior)
VALUES
    ('has_state', 'Has State', NULL, false, 1.0, 'builtin', 'supersede')
ON CONFLICT (rel_type) DO NOTHING;

-- 1b. has_state metadata (guarded — only fills the freshly-inserted skeleton)
UPDATE public.rel_types SET
    head_types          = ARRAY['ANY']::TEXT[],
    tail_types          = ARRAY['SCALAR']::TEXT[],
    is_symmetric        = false,
    inverse_rel_type    = NULL,
    is_hierarchy_rel    = false,
    fact_class          = 'B',
    storage_target      = 'entity_attributes',
    category            = 'state',
    temporal_class      = 'event',
    scalar_datatype     = 'string',
    natural_language    = 'X is in state Y',
    natural_language_2p = 'You are in state Y'
WHERE rel_type = 'has_state'
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
            EXECUTE format($ins$
                INSERT INTO %I.rel_types
                    (rel_type, label, wikidata_pid, engine_generated, confidence,
                     source, correction_behavior)
                VALUES
                    ('has_state', 'Has State', NULL, false, 1.0, 'builtin', 'supersede')
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);

            EXECUTE format($upd$
                UPDATE %I.rel_types SET
                    head_types          = ARRAY['ANY']::TEXT[],
                    tail_types          = ARRAY['SCALAR']::TEXT[],
                    is_symmetric        = false,
                    inverse_rel_type    = NULL,
                    is_hierarchy_rel    = false,
                    fact_class          = 'B',
                    storage_target      = 'entity_attributes',
                    category            = 'state',
                    temporal_class      = 'event',
                    scalar_datatype     = 'string',
                    natural_language    = 'X is in state Y',
                    natural_language_2p = 'You are in state Y'
                WHERE rel_type = 'has_state'
                  AND (head_types IS NULL OR head_types = '{}')
            $upd$, _schema);
        END IF;

        RAISE NOTICE 'Migration 110: minted has_state scalar predicate into %', _schema;
    END LOOP;
END $$;
