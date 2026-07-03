-- Migration 129: AGE is a TYPE-INDEPENDENT scalar (head_types = ANY).
-- Date: 2026-07-03
--
-- WHY
-- ---
-- "The server is 2 years old.", "My house is 100 years old.", "The wine is 10 years old." — a
-- server, house, product, animal, and person ALL have an age. Age (a DURATION since origin) is a
-- universal, type-independent measurement — unlike HEIGHT / WEIGHT, which are Person-physical.
--
-- But `age` was seeded with head_types = {Person}, so the deriver's copula-measure chain
-- (_chain_copula_measure → _scalar_rel_admits_subject, linguistics.py) STEPS ASIDE for a common-noun
-- subject whose GLiNER2 type (server→OBJECT, house→OBJECT/LOCATION) is not admitted by {Person}:
-- the explicit "N years old" age is DROPPED and the entity falls to a bare has_state. This veto was
-- built for the "the tomatoes are 2-3 inches tall" over-reach — but that is HEIGHT (Person-physical),
-- which correctly STAYS {Person} and keeps deferring. Only AGE is universal.
--
-- This also aligns the deriver veto with the WGM gate, which ALREADY admits non-Person age
-- (assign/validate: Person 0–150 strict; non-Person any non-negative). The {Person} head_types was
-- the sole remaining Person-scoping on age; widening it to {ANY} makes the explicit "N years old"
-- idiom bind the age scalar on ANY entity, while height/weight stay Person-scoped (tomatoes guard
-- preserved). Subject-agnostic, metadata-driven — NO deriver code change, NO rel-name code check.
--
-- SEEDING MODEL (identical to migrations 119 / 124): public.rel_types is the SEED SOURCE — NEW
-- tenants are blanket-copied from public (schema_manager.py), so updating public makes every new
-- tenant correct. EXISTING tenants are fixed by the per-schema loop in Part 2.
--
-- Idempotent: absolute-value UPDATE, re-runnable.
-- NOTE: after applying, FLUSH the overlay caches (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL so rel_type_overlay picks up the new head_types.

-- ============================================================================
-- Part 1: public (SEED-SOURCE / TEMPLATE)
-- ============================================================================
UPDATE public.rel_types SET head_types = '{ANY}'
WHERE rel_type = 'age';

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
        EXECUTE format($u$
            UPDATE %I.rel_types SET head_types = '{ANY}'
            WHERE rel_type = 'age'
        $u$, _schema);
        RAISE NOTICE 'Migration 129: age head_types -> {ANY} in %', _schema;
    END LOOP;
END $$;
