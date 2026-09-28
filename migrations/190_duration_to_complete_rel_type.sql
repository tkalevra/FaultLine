-- Migration 190: seed the semantic `duration` SCALAR rel_type (duration-to-complete capture)
-- Date: 2026-07-21
--
-- WHY
-- ---
-- The LongMemEval temporal-reasoning cluster states a DURATION-TO-COMPLETE — "…which took me three
-- weeks to finish", "it took me three weeks to finish the book", "I finished the report in two days".
-- The deterministic measure-verb deriver chain (linguistics.py `_chain_verb_measure`) already captures
-- the measure quantity as a SCALAR, but for the COMPLETION construction it (a) hung the value on the
-- light-verb clause subject — an expletive "it" or the relative pronoun "which" — instead of the
-- COMPLETED OBJECT, and (b) named the attribute with the raw verb lemma ("take"/"owns"), which is not
-- identifiable downstream as a DURATION (so a "how long did X take / sum the durations" question has no
-- semantic attribute to read). The deriver now (this session) routes the completion frame to the
-- COMPLETED entity via THIS seeded semantic `duration` scalar rel.
--
-- WHAT
-- ----
-- `duration` — a SCALAR rel (tail_types={SCALAR} → the value routes to entity_attributes, exactly like
-- age/height). head_types={ANY}: the completed thing is ANY type (a book, a project, a chore — never
-- Person-only). scalar_datatype='duration' is the SHAPE-FREE datatype (main.py `_validate_scalar_datatype`
-- accepts it verbatim), so a WORDED span ("three weeks") is stored as-is — a numeric 'quantity' would
-- reject a non-digit worded number. fact_class 'B' (a durable stated measure; entity_attributes has no
-- C-expiry regardless). temporal_class 'immutable' (a completed duration is fixed). correction_behavior
-- 'supersede' (a later restatement overwrites, like every other scalar on the UNIQUE(entity,attribute)).
--
-- Seeded into public (TEMPLATE/SEED-SOURCE) so new tenants inherit it via the provisioning bootstrap
-- (`INSERT INTO {schema}.rel_types SELECT * FROM public.rel_types`), and fanned out to EXISTING tenant
-- schemas below. Idempotent: ON CONFLICT (rel_type) DO NOTHING/UPDATE. Safe to re-run.
-- NOTE: after applying, FLUSH the rel_type overlay cache (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL so `_rel_tail_types('duration')` resolves SCALAR at the deriver's coverage gate.

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY)
-- ============================================================================
INSERT INTO public.rel_types
    (rel_type, label, engine_generated, confidence, source, correction_behavior,
     category, head_types, tail_types, fact_class, storage_target, is_hierarchy_rel,
     temporal_class, scalar_datatype)
VALUES
    ('duration', 'Duration to Complete', false, 1.0, 'builtin', 'supersede',
     'temporal', ARRAY['ANY'], ARRAY['SCALAR'], 'B', 'facts', false,
     'immutable', 'duration')
ON CONFLICT (rel_type) DO UPDATE SET
    category        = EXCLUDED.category,
    head_types      = EXCLUDED.head_types,
    tail_types      = EXCLUDED.tail_types,
    fact_class      = EXCLUDED.fact_class,
    scalar_datatype = EXCLUDED.scalar_datatype,
    temporal_class  = EXCLUDED.temporal_class,
    source          = EXCLUDED.source;

-- Human-readable render template (composer reads it; X = the completed entity, Y = the duration span).
UPDATE public.rel_types
   SET natural_language = 'X took Y to complete'
 WHERE rel_type = 'duration' AND natural_language IS NULL;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- New tenants inherit `duration` via the provisioning bootstrap copy from public.rel_types; this loop
-- fans it out to already-provisioned tenant schemas. Mirrors 016/061/109's fan-out. Idempotent.
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($seed$
            INSERT INTO %I.rel_types
                (rel_type, label, engine_generated, confidence, source, correction_behavior,
                 category, head_types, tail_types, fact_class, storage_target, is_hierarchy_rel,
                 temporal_class, scalar_datatype, natural_language)
            VALUES
                ('duration', 'Duration to Complete', false, 1.0, 'builtin', 'supersede',
                 'temporal', ARRAY['ANY'], ARRAY['SCALAR'], 'B', 'facts', false,
                 'immutable', 'duration', 'X took Y to complete')
            ON CONFLICT (rel_type) DO UPDATE SET
                category        = EXCLUDED.category,
                head_types      = EXCLUDED.head_types,
                tail_types      = EXCLUDED.tail_types,
                fact_class      = EXCLUDED.fact_class,
                scalar_datatype = EXCLUDED.scalar_datatype,
                temporal_class  = EXCLUDED.temporal_class,
                source          = EXCLUDED.source
        $seed$, _schema);

        RAISE NOTICE 'Migration 190: duration rel_type seeded into %', _schema;
    END LOOP;
END $$;
