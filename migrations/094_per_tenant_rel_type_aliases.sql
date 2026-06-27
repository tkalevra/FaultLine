-- Migration 094: Per-tenant rel_type_aliases + relationship ROLE-NOUN seeds
-- Date: 2026-06-15
-- Purpose: Make rel_type_aliases a PER-TENANT table so possessive-relationship anchor
--          resolution ("what is my mother's age" → the entity that fills (user, child_of, X))
--          can read the word→rel mapping UNQUALIFIED on the tenant search_path.
--
-- WHY THIS IS NEEDED
-- ------------------
-- The ingest/query request connection runs with `SET search_path TO {schema}` WITHOUT public
-- (commit 31580f6, public fallback removed to stop cross-schema leakage). rel_type_aliases
-- existed ONLY in public (migrations 030/031) and is therefore INVISIBLE at the tenant
-- search_path. A prior attempt read public.rel_type_aliases live — WRONG (it cannot resolve
-- in-tenant and re-introduces a public runtime read). Fix: seed rel_type_aliases INTO each
-- tenant from the public TEMPLATE; runtime reads the tenant copy ONLY. public is the SEED
-- SOURCE / template — never read at runtime.
--
-- PART (a): seed the relationship ROLE-NOUNS into public.rel_type_aliases (the SEED SOURCE /
--           template — adding to public is allowed; it is the template).  Bare role-nouns
--           (mother/father/son/daughter/wife/husband/boss/…) that migrations 030/031 missed.
--           Directionality (requires_inversion / inverse_alias) mirrors migration 031's
--           family convention.  Canonical rel_types are read from metadata at runtime; the
--           inverse PAIR (parent_of↔child_of) is completed from rel_types.inverse_rel_type,
--           so resolution is subject-agnostic (no hardcoded relation list in code).
--
-- PART (b): for every existing faultline_% tenant, CREATE rel_type_aliases if missing and
--           seed it from public.rel_type_aliases (only aliases whose canonical_rel_type is a
--           LIVE rel_type in that tenant — the FK + WHERE guard).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT (alias) DO NOTHING.
-- Safe to run repeatedly. No DROP, no destructive SQL. Reviewer applies live.

-- ============================================================================
-- PART (a): seed relationship ROLE-NOUNS into the public TEMPLATE
-- ============================================================================
-- Family role-nouns the user OWNS a relationship TO. "my mother" = user child_of mother;
-- canonical child_of, requires_inversion TRUE, inverse_alias parent_of — same convention
-- as migration 031's son_of/daughter_of. (Resolution walks BOTH directions over the
-- canonical∪inverse pair, so the stored direction does not change the answer; these flags
-- keep the row semantically consistent with the existing family seeds.)
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source) VALUES
    ('child_of',   'mother',   'ontology'),
    ('child_of',   'father',   'ontology'),
    ('child_of',   'mom',      'ontology'),
    ('child_of',   'mum',      'ontology'),
    ('child_of',   'dad',      'ontology'),
    ('child_of',   'parent',   'ontology'),
    ('parent_of',  'son',      'ontology'),
    ('parent_of',  'daughter', 'ontology'),
    ('parent_of',  'kid',      'ontology'),
    ('spouse',     'wife',     'ontology'),
    ('spouse',     'husband',  'ontology'),
    ('sibling_of', 'brother',  'ontology'),
    ('sibling_of', 'sister',   'ontology'),
    ('works_for',  'boss',     'ontology'),
    ('works_for',  'manager',  'ontology'),
    ('works_for',  'employer', 'ontology'),
    ('friend_of',  'friend',   'ontology')
ON CONFLICT (alias) DO NOTHING;

-- Directionality for the family role-nouns (mirrors migration 031).
-- mother/father/etc. → child_of WITH inversion (user is the child).
UPDATE public.rel_type_aliases SET
    requires_inversion = TRUE,
    is_symmetric = FALSE,
    inverse_alias = 'parent_of'
WHERE alias IN ('mother', 'father', 'mom', 'mum', 'dad', 'parent')
  AND source = 'ontology';

-- son/daughter/kid → parent_of WITHOUT inversion (user is the parent).
UPDATE public.rel_type_aliases SET
    requires_inversion = FALSE,
    is_symmetric = FALSE,
    inverse_alias = 'child_of'
WHERE alias IN ('son', 'daughter', 'kid')
  AND source = 'ontology';

-- wife/husband → spouse (symmetric).
UPDATE public.rel_type_aliases SET
    requires_inversion = FALSE,
    is_symmetric = TRUE,
    inverse_alias = 'spouse'
WHERE alias IN ('wife', 'husband')
  AND source = 'ontology';

-- brother/sister → sibling_of (symmetric).
UPDATE public.rel_type_aliases SET
    requires_inversion = FALSE,
    is_symmetric = TRUE,
    inverse_alias = 'sibling_of'
WHERE alias IN ('brother', 'sister')
  AND source = 'ontology';

-- friend → friend_of (symmetric).
UPDATE public.rel_type_aliases SET
    requires_inversion = FALSE,
    is_symmetric = TRUE,
    inverse_alias = 'friend_of'
WHERE alias = 'friend'
  AND source = 'ontology';

-- ============================================================================
-- PART (b): CREATE + seed rel_type_aliases into every existing faultline_% tenant
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- Create the per-tenant table if missing. Column set mirrors migrations 030 + 031.
        -- FK to the tenant's OWN rel_types (the FK resolves to %I.rel_types under the
        -- in-tenant DDL — no public reference).
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
            )$ddl$, _schema, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_alias ON %I.rel_type_aliases(alias)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_canonical ON %I.rel_type_aliases(canonical_rel_type)', _schema);

        -- Seed from the public template. Only aliases whose canonical_rel_type is a LIVE
        -- rel_type in THIS tenant (the FK would reject others; the WHERE guard avoids the
        -- error and keeps the copy clean). public is the seed SOURCE only.
        EXECUTE format($seed$
            INSERT INTO %I.rel_type_aliases
                (canonical_rel_type, alias, source, confidence,
                 requires_inversion, is_symmetric, inverse_alias)
            SELECT a.canonical_rel_type, a.alias, a.source, a.confidence,
                   a.requires_inversion, a.is_symmetric, a.inverse_alias
            FROM public.rel_type_aliases a
            WHERE a.canonical_rel_type IN (SELECT rel_type FROM %I.rel_types)
            ON CONFLICT (alias) DO NOTHING
        $seed$, _schema, _schema);

        RAISE NOTICE 'Migration 094: seeded rel_type_aliases into %', _schema;
    END LOOP;
END $$;
