-- Migration 142: linguistic_cues — seed the role_noun cue class (copula predicate-nominal role)
-- Date: 2026-07-06
--
-- WHY
-- ---
-- "<Filler NP> is my <role-noun>" ("Globex Industries is my employer") had NO owning deriver
-- chain: _chain_possessive read only "my employer" → (user, owns, "employer"), the copula SUBJECT
-- NP (the actual entity) was never bound, and the lone role noun got GLiNER2-mistyped downstream
-- (Animal → has_pet junk). The new _chain_copula_role_predicate (src/extraction/linguistics.py)
-- binds the SUBJECT NP as the filler entity via a METADATA role→rel_type map — this migration
-- seeds that map as a new KEYED cue class on the SAME (cue, category) rail as kinship_noun /
-- unit_scalar (migration 109 precedent): `cue` = the role noun lemma, `description` = the rel_type,
-- resolved by linguistic_cue_overlay.resolve_role_noun_map() into a {noun: rel_type} dict.
--
-- CONVENTION (deliberately a SEPARATE category — do NOT fold into social_role/kinship_noun):
-- the kinship/social_role maps run FILLER→user ("my mother" → (mother, parent_of, user)); the
-- role_noun map runs USER→FILLER ("Globex is my employer" → (user, works_for, globex)). Mixing
-- the two direction conventions in one category is a direction bug waiting to happen.
--
-- SEED IS SMALL (lean-seed discipline): only the universal employment primitives, all landing on
-- the SEEDED rel works_for (P108, head_types={Person}, tail_types={Organization,Person} — a Person
-- filler like "my boss Jane" is admitted). Growth adds domain roles per-tenant.
-- `employee` is INTENTIONALLY ABSENT: works_for has NO seeded inverse rel_type
-- (inverse_rel_type = NULL), so "<Name> is my employee" has no honest user→filler rel to map —
-- the chain no-ops rather than fabricating a relation.
--
-- The in-code map (_BOOTSTRAP_ROLE_NOUN_MAP, linguistic_cue_overlay.py) REMAINS as the DB-DOWN
-- code-fallback seed only; the live authority is the DB (per-tenant, overlay-resolved, growable).
--
-- NO DDL CHANGE: 105 created the table (public + per-tenant) general-by-category, and the
-- provisioning seeder (schema_manager.py) blanket-copies public.linguistic_cues into every NEW
-- tenant (role_noun is not in its carve-out exclusion list) — so new tenants inherit this class
-- automatically. This migration only (1) seeds public and (2) fans out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- Guard: if migration 105 has not run, create the table in public so this seed has a target. Same
-- DDL as 105/109 (idempotent).
CREATE TABLE IF NOT EXISTS public.linguistic_cues (
    id                SERIAL PRIMARY KEY,
    cue               VARCHAR(128) NOT NULL,
    category          VARCHAR(64)  NOT NULL DEFAULT 'naming_verb',
    frequency         INT   DEFAULT 0,
    confirmed_count   INT   DEFAULT 0,
    rejected_count    INT   DEFAULT 0,
    correction_count  INT   DEFAULT 0,
    global_confidence FLOAT DEFAULT 0.5,
    description       TEXT,
    example_text      TEXT,
    source            VARCHAR(64),
    is_active         BOOLEAN DEFAULT true,
    archived_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW(),
    last_matched_at   TIMESTAMP,
    UNIQUE (cue, category)
);

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the role_noun class
-- ============================================================================
-- All string literals SINGLE-QUOTED with '' escaping (109's hard lesson).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('employer',   'role_noun', 'works_for', 'Globex Industries is my employer', 'seed_role_noun', 0.92),
  ('boss',       'role_noun', 'works_for', 'Jane is my boss',                  'seed_role_noun', 0.85),
  ('manager',    'role_noun', 'works_for', 'Tom is my manager',                'seed_role_noun', 0.85),
  ('supervisor', 'role_noun', 'works_for', 'Rita is my supervisor',            'seed_role_noun', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (105 / user_schema.sql). Create it if missing
-- (defensive, same DDL), then seed the NEW category from public. Mirrors 109's fan-out. Idempotent.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- ---- table DDL (tenant-local, defensive) ----
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.linguistic_cues (
                id                SERIAL PRIMARY KEY,
                cue               VARCHAR(128) NOT NULL,
                category          VARCHAR(64)  NOT NULL DEFAULT 'naming_verb',
                frequency         INT   DEFAULT 0,
                confirmed_count   INT   DEFAULT 0,
                rejected_count    INT   DEFAULT 0,
                correction_count  INT   DEFAULT 0,
                global_confidence FLOAT DEFAULT 0.5,
                description       TEXT,
                example_text      TEXT,
                source            VARCHAR(64),
                is_active         BOOLEAN DEFAULT true,
                archived_at       TIMESTAMP,
                created_at        TIMESTAMP DEFAULT NOW(),
                updated_at        TIMESTAMP DEFAULT NOW(),
                last_matched_at   TIMESTAMP,
                UNIQUE (cue, category)
            )
        $ddl$, _schema);

        EXECUTE format($ix1$
            CREATE INDEX IF NOT EXISTS idx_linguistic_cues_active
                ON %I.linguistic_cues(is_active, category)
        $ix1$, _schema);
        EXECUTE format($ix2$
            CREATE INDEX IF NOT EXISTS idx_linguistic_cues_category
                ON %I.linguistic_cues(category)
        $ix2$, _schema);

        -- ---- seed the NEW category from public (template) ----
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category = 'role_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 142: role_noun cues seeded into %', _schema;
    END LOOP;
END $$;
