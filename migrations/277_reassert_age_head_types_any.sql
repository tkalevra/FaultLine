-- Migration 277: re-assert migration 129's `age.head_types = {ANY}` where a box carries {Person}
-- Date: 2026-09-16 (gauntlet l4-type-constraints-enforced, owner ruling 2026-09-16)
--
-- WHY
-- ---
-- Migration 129 widened the seeded `age` scalar's head_types from {Person} to {ANY} (an age is a
-- duration since origin — a server, a dog, a product all have one) in public AND every tenant.
-- Under the boot-migration ledger a migration runs ONCE per schema and is then stamped applied.
-- The old 016 seed (now 017_seed_builtin_scalar_rel_types.sql) failed on every fresh FIRST boot
-- (its head_types column arrived one file later), so the ledger recorded it `failed`, re-ran it on
-- boot #2 — AFTER 129 had already run and been stamped — and its unguarded DO UPDATE clobbered
-- age.head_types back to {Person}. 129 is stamped applied, so it never runs again: a ledger-era
-- fresh box built before 19a47226 (which guarded that re-run to the untyped-row convention)
-- carries age.head_types = {Person} PERMANENTLY, and every tenant provisioned from it inherits
-- the narrow value. Legacy-swept boxes (production, pre-prod) carry {ANY} and are untouched.
--
-- Measured on this corpus (2026-09-16): a fresh box booted three times now lands {ANY} on every
-- boot (the 017 guard holds), so this file is a NO-OP there — it exists for the boxes built in
-- the window, and it cannot know which those are except by reading the row.
--
-- WHAT
-- ----
-- Re-assert {ANY} on the SEEDED `age` row (rel_types.source ∈ wikidata|builtin — the seed's own
-- provenance column, the same tuple src/api/canonical.py::SEEDED_REL_SOURCES owns for Python
-- readers; a tenant-authored `source='user'` row is never touched) in public and in every tenant
-- schema, ONLY where the row is not already unconstrained. {ANY} is the widest possible head
-- constraint, so this is a UNION-widening by construction: a tenant that grew {Person, Animal}
-- is widened, never narrowed, and a row that already carries ANY is left alone (0 rows on a
-- healthy box). Idempotent on a three-boot box (second run: 0 rows everywhere).
--
-- NOTE: after applying, the rel_type overlay picks the value up within its 5s TTL.

-- ── 1. public (the template / seed source) ──────────────────────────────────
UPDATE public.rel_types
   SET head_types = ARRAY['ANY']::TEXT[]
 WHERE rel_type = 'age'
   AND source IN ('wikidata', 'builtin')
   AND NOT EXISTS (SELECT 1 FROM unnest(COALESCE(head_types, '{}')) h WHERE LOWER(h) = 'any');

-- ── 2. every tenant schema (per-tenant fan-out; grown/user rows untouched) ──
DO $$
DECLARE
    _schema TEXT;
    _n      INTEGER;
BEGIN
    FOR _schema IN
        SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\_%'
    LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = _schema AND table_name = 'rel_types') THEN
            CONTINUE;
        END IF;
        EXECUTE format($u$
            UPDATE %I.rel_types
               SET head_types = ARRAY['ANY']::TEXT[]
             WHERE rel_type = 'age'
               AND source IN ('wikidata', 'builtin')
               AND NOT EXISTS (SELECT 1 FROM unnest(COALESCE(head_types, '{}')) h
                               WHERE LOWER(h) = 'any')
        $u$, _schema);
        GET DIAGNOSTICS _n = ROW_COUNT;
        IF _n > 0 THEN
            RAISE NOTICE 'Migration 277: age head_types -> {ANY} in % (% row)', _schema, _n;
        END IF;
    END LOOP;
END $$;
