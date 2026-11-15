-- 296_rel_type_aliases_bare_plural_kin.sql
-- Seed the BARE PLURAL kin nouns into rel_type_aliases — public template AND every tenant
-- schema (#wound: "who are my children?").
--
-- WHY
-- ---
-- The query keyword→rel loop (determine_path) resolves a question's aspect word ONLY by an exact
-- rel_type name or a per-tenant rel_type_aliases row (indexed equality; tenant search_path is
-- bound WITHOUT public, so public rows alone never serve a live query). The seed carried the
-- possessed/verb forms (has_child/has_children/has_son/…→parent_of) and the role nouns
-- (kid→parent_of, son/daughter→parent_of, child→child_of), but NOT the bare plurals a real
-- question uses: "who are my CHILDREN / kids / sons / daughters?". Measured live on prod (seat
-- 3f2a9c41, 2026-10-09, after the #227-#231 deploy): "my children are: Marco, a son 12;
-- Leo, a son 20; Chiara, a daughter 10" captured PERFECTLY — (marco|leo|chiara,
-- child_of, user) + the three ages — but the recall of "who are my children" bound NO
-- relationship rel (no 'children' alias row in the tenant), the aspect fell through to a junk
-- grown scalar attribute, and topic_grounding_filtered dropped 8→2, leaving only stale junk
-- rows. Capture fixed; recall projected the answer out. The missing piece is this alias row.
--
-- UNIVERSAL SHAPE (per the 277/272 discipline): rel_type_aliases is a per-tenant table read
-- tenant-only at query time, so public (the provisioning template) is seeded FIRST and every
-- existing faultline_% schema is fanned out to explicitly — a psql migration file runs ONCE
-- against the DSN default schema, so without the loop only public would receive the rows and
-- every EXISTING tenant would stay wounded. Idempotent everywhere: ON CONFLICT (alias)
-- DO NOTHING per schema — a tenant that grew its own alias (e.g. engine-grown
-- children→parent_of via aspect-synonym growth) keeps its row untouched.
--
-- Direction: bare plural kin from the speaker's mouth name the SPEAKER'S dependants — the same
-- reading the existing seeds give "kid"/"has_children" (parent_of). The walk resolves the stored
-- (child, child_of, parent) rows through the rel directionality metadata (migration 031).
--
-- DATA, NOT CODE: exact alias rows on the same indexed table the typed walk reads; no in-code
-- literal, no fuzziness, no per-seat special case.

-- ── 1. public (the template / seed source for future tenants) ───────────────
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence) VALUES
  ('parent_of', 'children', 'ontology', 1.0),
  ('parent_of', 'kids',      'ontology', 1.0),
  ('parent_of', 'sons',      'ontology', 1.0),
  ('parent_of', 'daughters', 'ontology', 1.0)
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
                SELECT 'parent_of', v.alias, 'ontology', 1.0
                FROM (VALUES ('children'), ('kids'), ('sons'), ('daughters')) AS v(alias)
                WHERE EXISTS (
                    SELECT 1 FROM %I.rel_types r WHERE r.rel_type = 'parent_of'
                )
                ON CONFLICT (alias) DO NOTHING
            $seed$, _schema, _schema);
            RAISE NOTICE 'Migration 296: bare plural kin aliases seeded in %', _schema;
        EXCEPTION WHEN others THEN
            RAISE NOTICE 'Migration 296: skipped % (%)', _schema, SQLERRM;
        END;
    END LOOP;
END $$;
