-- Migration 146: linguistic_cues — seed the alias_predicate cue class (phrasal nickname/alias idioms)
-- Date: 2026-07-07
--
-- WHY
-- ---
-- The spine deriver captured a FIRST-PERSON self-name ("I prefer to be called Max") and the
-- "<role>'s name is <PROPN>" frame, but a THIRD-PARTY nickname stated with a PHRASAL alias idiom —
-- "she goes by Dee", "he is known as Sammy", "she is referred to as Liv" — was DROPPED, and the
-- intransitive/copula-state chains mis-minted a junk (she, has_state, go) / (she, has_state, know)
-- twin off the idiom's verb. The new deriver chain _chain_alias_predicate
-- (src/extraction/linguistics.py) captures these as (person, also_known_as, <Name>), resolving a
-- 3rd-person pronoun subject to the nearest preceding named person via _person_coref.
--
-- Two grammatical shapes feed that chain:
--   (A) a NAMING VERB (naming_verb cue class — call/name/title/…) taking the name as a direct
--       complement ("prefers to be called <Name>") — already DB-held; no new data.
--   (B) a PHRASAL ALIAS IDIOM — a verb + a specific licensing PREPOSITION governing a proper-noun
--       object ("go BY Dee", "known AS Sammy", "referred to AS Liv"). "go"/"know"/"refer" are NOT
--       naming verbs (they take no name as a direct object), so the alias reading needs the
--       LICENSING PARTICLE as the disambiguator. This migration seeds that verb→particle MAP as a
--       new KEYED cue class on the SAME (cue, category) rail as kinship_noun / unit_scalar / role_noun
--       (migration 109/142 precedent): `cue` = the verb lemma, `description` = the licensing particle,
--       resolved by linguistic_cue_overlay.resolve_alias_predicate_map() into a {verb: particle} dict.
--
-- WHY A LICENSING PARTICLE (not a bare verb set): "she GOES BY Dee" (alias) and "she GOES to work"
-- (motion) share the verb; only the particle ("by") + a PROPER-NOUN pobj distinguishes the alias
-- reading. The particle IS the safety gate that keeps the chain from over-capturing. Mirrors the
-- codebase's existing go-by nickname idiom (linguistics._nickname_run).
--
-- SEED IS SMALL (lean-seed discipline): the universal English alias idioms only (go→by, know→as,
-- refer→as). Growth adds more per-tenant, freq-gated. The in-code map
-- (_BOOTSTRAP_ALIAS_PREDICATE_MAP, linguistic_cue_overlay.py) REMAINS as the DB-DOWN code-fallback
-- seed only; the live authority is the DB (per-tenant, overlay-resolved, growable).
--
-- NO DDL CHANGE: 105 created the table (public + per-tenant) general-by-category, and the
-- provisioning seeder (schema_manager.py) blanket-copies public.linguistic_cues into every NEW
-- tenant (alias_predicate is NOT in its carve-out exclusion list) — so new tenants inherit this
-- class automatically. This migration only (1) seeds public and (2) fans out to EXISTING tenant
-- schemas. Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- Guard: if migration 105 has not run, create the table in public so this seed has a target. Same
-- DDL as 105/109/142 (idempotent).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the alias_predicate class
-- ============================================================================
-- `cue` = the verb lemma; `description` = the LICENSING PREPOSITION the verb must govern (with a
-- PROPN pobj) for the alias reading. All string literals SINGLE-QUOTED with '' escaping (109's lesson).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('go',    'alias_predicate', 'by', 'she goes by Dee',           'seed_alias_predicate', 0.90),
  ('know',  'alias_predicate', 'as', 'he is known as Sammy',      'seed_alias_predicate', 0.88),
  ('refer', 'alias_predicate', 'as', 'she is referred to as Liv', 'seed_alias_predicate', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (105 / user_schema.sql). Create it if missing
-- (defensive, same DDL), then seed the NEW category from public. Mirrors 109/142's fan-out. Idempotent.

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
            WHERE category = 'alias_predicate'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 146: alias_predicate cues seeded into %', _schema;
    END LOOP;
END $$;
