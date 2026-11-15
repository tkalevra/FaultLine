-- Migration 325: seed the QUERY-SIDE role-noun aliases for the blended-family kin titles
-- (the wound-map round, 2026-11-13) + REPAIR migration 320's alias omission.
--
-- WHY
-- ---
-- The blended-family FINAL-VERDICT (DEV/BUGS/blended-family-smoke-20261112) measured the
-- ex-wife-name miss: "the ex_spouse edge lands and the seat carries the cue rows, but
-- 'what's my ex-wife's name?' serves honest-empty". TWO query-side gaps, both DATA:
--
--   1. THE EX/STEP ROLE-NOUNS HAVE NO QUERY-SIDE ALIAS AT ALL. Migration 320 seeded the
--      kinship_noun CUES ('ex-wife' → ex_spouse — the CAPTURE rail) but NOTHING seeds the
--      rel_type row or the rel_type_aliases row the QUERY-side walk reads: the typed
--      multi-hop walk's hop "ex-wife" resolves through _relword_to_rel_candidates, which
--      reads the TENANT rel_type_aliases (UNQUALIFIED under the request search_path —
--      public is excluded). With no alias row the hop DEFERS and the legacy lane serves
--      honest-empty. (The ex_spouse rel_type itself exists on live seats only because the
--      engine-growth helper minted it at first capture — a seat that never captured an ex
--      turn has no rel_types row for the alias FK to reference.)
--
--   2. MIGRATION 320'S BIRTHDAY ALIAS LANDED PUBLIC-ONLY. Its Part-2 INSERT writes
--      public.rel_type_aliases and its fan loop copies ONLY the linguistic_cues. The
--      birthday→born_on alias therefore exists in the seed SOURCE but not in any tenant
--      schema provisioned BEFORE migration 320 ran (the provisioning bootstrap copies the
--      public template at provisioning time, so only seats minted AFTER 320 have it).
--      "when is my wife's birthday?" → the walk's scalar terminal probes the TENANT alias
--      table → miss → the legacy kin-prose lane wins ("Maren is your pediatric nurse")
--      while the born_on row sits unvoiced — the FINAL-VERDICT's wife-bday wound shape.
--
-- THE FIX (data, not code — the 094/319 discipline): seed the rel_types rows (guarded),
-- seed the role-noun aliases into the public TEMPLATE, and fan BOTH + the birthday alias
-- into every EXISTING faultline_% tenant. The walk itself needs no code change: it already
-- resolves candidates from the alias table and reads BOTH storage directions over the
-- canonical∪inverse rel pair (the capture convention files (named_person, rel, possessor),
-- the walk's owner-as-object arm reads exactly that).
--
-- NO DDL CHANGE beyond the idempotent CREATE TABLE IF NOT EXISTS guards (the 094/319
-- pattern) for tenant schemas predating the tables. Safe to re-run: every insert is
-- ON CONFLICT DO NOTHING (rel_types PK; rel_type_aliases UNIQUE(alias)).

-- ============================================================================
-- Part 1: public.rel_types — the canonical kin rels the aliases reference (guarded; a
-- seat whose engine already minted the row keeps its own metadata untouched).
-- ============================================================================
INSERT INTO public.rel_types
    (rel_type, label, source, engine_generated, confidence, correction_behavior, category)
VALUES
    ('ex_spouse',     'Ex-Spouse',      'builtin', false, 1.0, 'supersede', 'family'),
    ('step_parent_of','Step-Parent Of', 'builtin', false, 1.0, 'supersede', 'family')
ON CONFLICT (rel_type) DO NOTHING;

UPDATE public.rel_types SET category = 'family'
WHERE rel_type IN ('ex_spouse', 'step_parent_of') AND (category IS NULL OR category = '');

-- ============================================================================
-- Part 2: public.rel_type_aliases (TEMPLATE / SEED-SOURCE ONLY) — the ex/step role-noun
-- surfaces. GUARDED inserts: each row lands only when its canonical rel_type exists in
-- public.rel_types (partial-safe + idempotent by UNIQUE (alias)).
-- Directionality mirrors migration 094's family rows AND migration 320's capture
-- convention (the kin rail files (named_person, rel, possessor) — the possessor is the
-- OBJECT of the stored edge, so requires_inversion=TRUE; the walk resolves BOTH storage
-- directions over the canonical∪inverse pair regardless).
-- ============================================================================
INSERT INTO public.rel_type_aliases
    (canonical_rel_type, alias, source, confidence, requires_inversion, is_symmetric, inverse_alias)
SELECT v.canonical, v.alias, 'seed_blended_query_side', 0.95, TRUE, FALSE, v.canonical
FROM (VALUES
    ('ex_spouse',      'ex-wife'),
    ('ex_spouse',      'ex wife'),
    ('ex_spouse',      'ex-husband'),
    ('ex_spouse',      'ex husband'),
    ('step_parent_of', 'stepson'),
    ('step_parent_of', 'step-son'),
    ('step_parent_of', 'stepdaughter'),
    ('step_parent_of', 'step-daughter')
) AS v(canonical, alias)
WHERE EXISTS (SELECT 1 FROM public.rel_types r WHERE r.rel_type = v.canonical)
ON CONFLICT (alias) DO NOTHING;

-- ============================================================================
-- Part 3: fan into every EXISTING faultline_% tenant — the rel_types rows, the role-noun
-- aliases, AND migration 320's birthday→born_on alias (the omission repair). Guarded per
-- tenant: a partially-provisioned seat is SKIPPED, never aborts the whole DO block.
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
        -- Partially-provisioned seat (no rel_types yet) → SKIP, never abort the loop
        -- (the 320 pattern; measured live: a tenant schema can exist pre-rel_types).
        IF to_regclass(format('%I.rel_types', _schema)) IS NULL THEN
            CONTINUE;
        END IF;
        -- 3a. the tenant rel_types rows (the alias FK resolves INSIDE the tenant).
        EXECUTE format($rt$
            INSERT INTO %I.rel_types
                (rel_type, label, source, engine_generated, confidence,
                 correction_behavior, category)
            VALUES
                ('ex_spouse',      'Ex-Spouse',      'builtin', false, 1.0, 'supersede', 'family'),
                ('step_parent_of', 'Step-Parent Of', 'builtin', false, 1.0, 'supersede', 'family')
            ON CONFLICT (rel_type) DO NOTHING
        $rt$, _schema);

        -- 3b. the tenant rel_type_aliases table (094's guarded DDL for older schemas) +
        -- the role-noun aliases + the birthday alias repair. The alias insert is guarded
        -- on THIS tenant's own rel_types carrying the canonical rel (the FK would abort
        -- the loop otherwise). Birthday: only when the tenant carries born_on (the same
        -- guard migration 320 used on the public template).
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.rel_type_aliases (
                id                 SERIAL PRIMARY KEY,
                canonical_rel_type VARCHAR(255) NOT NULL,
                alias              VARCHAR(255) NOT NULL UNIQUE,
                created_at         TIMESTAMP DEFAULT NOW(),
                source             VARCHAR(50) DEFAULT 'ontology',
                confidence         FLOAT DEFAULT 1.0,
                requires_inversion BOOLEAN DEFAULT FALSE,
                is_symmetric       BOOLEAN DEFAULT FALSE,
                inverse_alias      VARCHAR(255),
                FOREIGN KEY (canonical_rel_type)
                    REFERENCES %I.rel_types(rel_type) ON DELETE CASCADE
            )
        $ddl$, _schema, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_alias ON %I.rel_type_aliases(alias)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_canonical ON %I.rel_type_aliases(canonical_rel_type)', _schema);

        EXECUTE format($seed$
            INSERT INTO %I.rel_type_aliases
                (canonical_rel_type, alias, source, confidence,
                 requires_inversion, is_symmetric, inverse_alias)
            SELECT v.canonical, v.alias, 'seed_blended_query_side', 0.95, TRUE, FALSE, v.canonical
            FROM (VALUES
                ('ex_spouse',      'ex-wife'),
                ('ex_spouse',      'ex wife'),
                ('ex_spouse',      'ex-husband'),
                ('ex_spouse',      'ex husband'),
                ('step_parent_of', 'stepson'),
                ('step_parent_of', 'step-son'),
                ('step_parent_of', 'stepdaughter'),
                ('step_parent_of', 'step-daughter')
            ) AS v(canonical, alias)
            WHERE EXISTS (SELECT 1 FROM %I.rel_types r WHERE r.rel_type = v.canonical)
            ON CONFLICT (alias) DO NOTHING
        $seed$, _schema, _schema);

        -- THE 320-OMISSION REPAIR: the birthday→born_on alias into every existing tenant.
        EXECUTE format($bday$
            INSERT INTO %I.rel_type_aliases (canonical_rel_type, alias, source, confidence)
            SELECT 'born_on', 'birthday', 'seed_blended_family_kinship', 0.95
            WHERE EXISTS (SELECT 1 FROM %I.rel_types r WHERE r.rel_type = 'born_on')
            ON CONFLICT (alias) DO NOTHING
        $bday$, _schema, _schema);
    END LOOP;
END $$;
