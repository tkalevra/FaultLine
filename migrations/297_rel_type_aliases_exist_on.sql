-- 297_rel_type_aliases_exist_on.sql
-- WOUND D: normalize the lifted verb `exist_on` to the canonical co-location rel `member_of`
-- (public template + every tenant schema).
--
-- WHY
-- ---
-- Measured on prod (seat 10d7d879, laptop turn): "My laptop … exists on tailscale as the same
-- name" lifted the SVO predicate `exist_on` (the verb lemma + the load-bearing prep, at
-- `_svo_predicate_token` — the lift itself is correct) and filed it VERBATIM as the rel_type.
-- The canonical lanes carry run_on/member_of-style co-location rels; a non-canonical `exist_on`
-- mints a novel rel the aspect→rel walk never reads.
--
-- FIX AT THE NORMALIZATION SPOT, NOT A WORD LIST IN CODE: the ingest edge lane already canonicalizes
-- every lifted rel through the tenant `rel_type_aliases` table (dprompt-125,
-- `_get_canonical_rel_type`); the missing piece is this alias row. `member_of` is the seeded
-- canonical co-location rel (migrations 021/030) — "X exists on Y" is X's membership in Y's
-- co-location domain, exactly the reading member_of already carries for network/team memberships.
-- Both surface spellings are seeded (the lemma is `exist`; some lanes lift the inflected surface).
--
-- UNIVERSAL SHAPE (the 296 discipline): rel_type_aliases is a per-tenant table read tenant-only at
-- ingest/query, so public is seeded FIRST and every existing faultline_% schema is fanned out
-- explicitly. Idempotent everywhere: ON CONFLICT (alias) DO NOTHING — a tenant that grew its own
-- alias keeps its row. DATA, NOT CODE: exact alias rows on the indexed table the normalization
-- reads; no in-code literal.

-- ── 1. public (the template / seed source for future tenants) ───────────────
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence) VALUES
  ('member_of', 'exist_on',  'ontology', 1.0),
  ('member_of', 'exists_on', 'ontology', 1.0)
ON CONFLICT (alias) DO NOTHING;

-- ── 2. every tenant schema (per-tenant fan-out; grown/user rows untouched) ──
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\_%'
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_type_aliases'
        ) THEN
            CONTINUE;
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.tables t
            WHERE t.table_schema = _schema AND t.table_name = 'rel_types'
        ) THEN
            CONTINUE;
        END IF;
        BEGIN
            EXECUTE format($seed$
                INSERT INTO %I.rel_type_aliases (canonical_rel_type, alias, source, confidence)
                SELECT 'member_of', v.alias, 'ontology', 1.0
                FROM (VALUES ('exist_on'), ('exists_on')) AS v(alias)
                WHERE EXISTS (
                    SELECT 1 FROM %I.rel_types r WHERE r.rel_type = 'member_of'
                )
                ON CONFLICT (alias) DO NOTHING
            $seed$, _schema, _schema);
            RAISE NOTICE 'Migration 297: exist_on co-location aliases seeded in %', _schema;
        EXCEPTION WHEN others THEN
            RAISE NOTICE 'Migration 297: skipped % (%)', _schema, SQLERRM;
        END;
    END LOOP;
END $$;
