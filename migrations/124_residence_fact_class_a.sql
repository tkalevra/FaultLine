-- Migration 124: user-stated RESIDENCE is authoritative (Class A).
-- Date: 2026-07-01
--
-- WHY
-- ---
-- A user directly STATING where they live ("I live at 156 Cedar St. S", "I live in Riverton")
-- is grounded user truth — the same tier as name / identity / birthdate. But `lives_at` and
-- `lives_in` were seeded at fact_class='B' (behavioral, staged, promote-on-confirmation), so a
-- user-stated residence landed in staged_facts and had to be repeated 3× before it committed
-- authoritatively. Per the class model (assign_class_and_confidence): a `user_stated` fact is
-- Class A IFF the rel's DEFINED fact_class is A, else Class B. Bumping the defined class to A makes
-- a stated residence commit write-through (Class A, confidence 1.0), matching identity.
--
-- SCOPE (deliberate): ONLY the animate-resident residence rels `lives_at` / `lives_in` are bumped.
-- The GEOGRAPHIC CONTAINMENT chain (`located_in`, `located_at`) stays at its current tier — a
-- city-in-province / server-in-rack containment edge is engine-grown structure, not a user
-- identity claim, so it is intentionally NOT promoted to A here.
--
-- SEEDING MODEL (identical to migration 119): public.rel_types is the SEED SOURCE — NEW tenants are
-- blanket-copied from public (schema_manager.py), so updating public makes every new tenant correct.
-- EXISTING tenants are fixed by the per-schema loop in Part 2.
--
-- Idempotent: absolute-value UPDATEs, re-runnable.
-- NOTE: after applying, FLUSH the overlay caches (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL so rel_type_overlay picks up the new fact_class.

-- ============================================================================
-- Part 1: public (SEED-SOURCE / TEMPLATE)
-- ============================================================================
UPDATE public.rel_types SET fact_class = 'A'
WHERE rel_type IN ('lives_at', 'lives_in');

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
            UPDATE %I.rel_types SET fact_class = 'A'
            WHERE rel_type IN ('lives_at', 'lives_in')
        $u$, _schema);
        RAISE NOTICE 'Migration 124: lives_at/lives_in -> Class A in %', _schema;
    END LOOP;
END $$;
