-- Migration 130: reconcile SEEDED rel_types' STRUCTURAL FLAGS back to the public seed.
-- Date: 2026-07-03
--
-- WHY
-- ---
-- AUTHORITY ORDER — user > SEED > engine-growth. A rel present in the public template is the
-- authoritative STRUCTURAL ontology (is_hierarchy_rel / category / tail_types / fact_class /
-- storage_target / inverse_rel_type / is_symmetric). Engine GROWTH (±6 climb, re-embedder
-- ontology-evaluation / class-C-promotion / synonym-convergence, in-flow rel mint) may PROPOSE +
-- ADD (mint novel rels, WIDEN head_types by union, strengthen confidence) but must NEVER OVERRIDE
-- a seeded rel's structural classification — the same truth-firewall as THE HARD LINE (the engine
-- grows PLACES, never corrupts the grounded definition).
--
-- OBSERVED DRIFT (the bug this reconciles): in a subset of tenants, growth mutated the SEEDED
-- `owns` (public: is_hierarchy_rel=false, category NULL, no inverse) to
-- is_hierarchy_rel=true / category=family / inverse_rel_type=owned_by. is_hierarchy_rel drives
-- storage routing (classify_fact_type: hierarchy→facts HIERARCHICAL vs relational) + traversal +
-- possession-rel resolution, so the flip broke name-intent recall. The write-side guard
-- (_seed_structural_flags in main.py / embedder.py, this session) PREVENTS new drift by pinning a
-- seeded rel's structural fields to the public seed before every growth write; this migration
-- RECONCILES tenants that ALREADY drifted.
--
-- KG grounding: in RDF Schema, rdf:type (Wikidata P31, the is_hierarchy_rel individual→class axis)
-- and rdfs:subClassOf (P279, class→class taxonomy) are DEFINITIONAL axes fixed by the ontology
-- author, not derivable / mutable from instance data — https://www.w3.org/TR/rdf-schema/#ch_type
-- and #ch_subclassof. So a seeded rel's is_hierarchy_rel is authoritative; a growth pass that
-- flipped it is a category error to be undone.
--
-- SCOPE / SAFETY
-- -------------
--  * ONLY rels PRESENT in public.rel_types are touched (join on rel_type) — genuinely novel
--    per-tenant grown rels (absent from public) are left completely alone.
--  * head_types is DELIBERATELY NOT reset — growth may legitimately WIDEN it by union (additive);
--    reverting it would drop a valid widening. Only the FROZEN structural fields are reconciled.
--  * A USER-asserted override (tenant row source='user', set only by POST /ontology/rel_types) is
--    NEVER reverted — user > seed. The `lower(COALESCE(t.source,'')) <> 'user'` gate preserves it.
--    (Growth-drifted rows keep their seed source 'builtin'/'wikidata', so they are eligible.)
--  * public.rel_types is the SEED-SOURCE reference here (fully-qualified, always accessible
--    regardless of tenant search_path). New tenants are blanket-copied from public at provisioning,
--    so the seed itself needs no change — only existing drifted tenants do (Part 1).
--
-- Idempotent: the IS DISTINCT FROM guard makes a re-run a no-op once a schema matches the seed.
-- NOTE: after applying, FLUSH the overlay caches (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL so rel_type_overlay picks up the reconciled structural flags.

-- ============================================================================
-- Part 1: Per-user schemas (loop over faultline_* schemas) — reset drifted seeds
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
    _n      BIGINT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($u$
            UPDATE %I.rel_types t
            SET is_hierarchy_rel = p.is_hierarchy_rel,
                category         = p.category,
                tail_types       = p.tail_types,
                fact_class       = p.fact_class,
                storage_target   = p.storage_target,
                inverse_rel_type = p.inverse_rel_type,
                is_symmetric     = p.is_symmetric
            FROM public.rel_types p
            WHERE t.rel_type = p.rel_type
              AND lower(COALESCE(t.source, '')) <> 'user'
              AND (t.is_hierarchy_rel IS DISTINCT FROM p.is_hierarchy_rel
                OR t.category         IS DISTINCT FROM p.category
                OR t.tail_types       IS DISTINCT FROM p.tail_types
                OR t.fact_class       IS DISTINCT FROM p.fact_class
                OR t.storage_target   IS DISTINCT FROM p.storage_target
                OR t.inverse_rel_type IS DISTINCT FROM p.inverse_rel_type
                OR t.is_symmetric     IS DISTINCT FROM p.is_symmetric)
        $u$, _schema);
        GET DIAGNOSTICS _n = ROW_COUNT;
        IF _n > 0 THEN
            RAISE NOTICE 'Migration 130: reconciled % seeded rel_type(s) to public in %', _n, _schema;
        END IF;
    END LOOP;
END $$;
