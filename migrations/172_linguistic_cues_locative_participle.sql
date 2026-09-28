-- Migration 172: linguistic_cues — seed the LOCATIVE-PARTICIPLE cue class onto the SAME rail
-- (category='locative_participle').
-- Date: 2026-07-12
--
-- WHY
-- ---
-- The copula/passive CONTAINMENT idiom "<X> is <participle> in/at/on/within/inside <place>" is how a
-- dumped fleet/inventory expresses where a thing SITS ("Rack-2 IS LOCATED IN row-a", "the server IS
-- SITUATED IN dc-toronto", "core-1 IS MOUNTED IN rack-1"). On the spine this captured WRONG:
--   • the generic SVO folded a NOVEL, fragile ``locate_in`` predicate (not the seeded ``located_in``
--     containment hierarchy rel), and
--   • when the pobj mis-POSes (spaCy tags a hyphenated label "row-a" as ADV) the object was DROPPED
--     and the clause collapsed to junk ``(rack-2, has_state, locate)``.
-- A new deterministic deriver chain ``linguistics.derive_sentence_facts._chain_copula_locative``
-- recognizes the construction and emits ``located_in(<X>, <place>)`` (is_hierarchy_rel) — the SAME
-- containment edge "the server is in rack 4" already produces.
--
-- The trigger is fully grammatical (a participle governing a CONTAINMENT/locative prep with a nominal
-- pobj) GATED by this LOCATIVE-PARTICIPLE cue class — the discriminator that keeps the containment
-- reading OFF a non-locative passive+prep ("is WRITTEN in Python", "is MADE in China"), whose "in" is
-- NOT a container. A locative-passive reading cannot be made purely structural from the preposition
-- alone; the participle vocabulary + the containment-prep parse gate is the discriminator. This is a
-- ⚠️ FLAGGED BOUNDED LEXICAL CLASS (exactly like naming/relocation/acquisition/employment).
--
-- The class is resolved from ``<tenant>.linguistic_cues`` (category='locative_participle') via the SAME
-- per-tenant overlay the naming/relocation/temporal layers use — DB-HELD + per-tenant + GROWABLE, NOT a
-- frozen in-code list. The in-code ``linguistics._LOCATIVE_PARTICIPLE_LEMMAS`` /
-- ``linguistic_cue_overlay._BOOTSTRAP_LOCATIVE_PARTICIPLES`` frozensets REMAIN only as the DB-DOWN
-- code-fallback seed (fail-safe; never lose detection on a pre-migration / unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (nsubjpass/acl/prep/pobj),
-- the participle morphology test (VerbForm=Part / passive), and the containment-prep set
-- (in/at/on/within/inside — a closed adposition primitive). Only the LOCATIVE-PARTICIPLE lemma
-- vocabulary is DB data that grows.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant); the provisioning seeder
-- (schema_manager.py) blanket-copies ALL non-carved public.linguistic_cues categories into every NEW
-- tenant — so new tenants inherit this class automatically. This migration only (1) seeds the new
-- category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/167).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the locative-participle class
-- ============================================================================
-- `cue` is matched against the spaCy token LEMMA (participles), lowercase. Membership is corroborated
-- downstream by the parse (participle + a containment/locative prep whose pobj is a nominal PLACE).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('locate',   'locative_participle', 'Locative participle: "<X> is located in <place>" → located_in',     'Rack-2 is located in row-a',        'seed_locative', 0.92),
  ('situate',  'locative_participle', 'Locative participle: "<X> is situated in <place>" → located_in',     'The server is situated in dc-1',     'seed_locative', 0.90),
  ('position', 'locative_participle', 'Locative participle: "<X> is positioned in <place>" → located_in',   'The sensor is positioned in bay-2',  'seed_locative', 0.82),
  ('house',    'locative_participle', 'Locative participle: "<X> is housed in <place>" → located_in',       'The array is housed in cabinet-3',   'seed_locative', 0.82),
  ('install',  'locative_participle', 'Locative participle: "<X> is installed in <place>" → located_in',    'core-1 is installed in rack-1',      'seed_locative', 0.85),
  ('instal',   'locative_participle', 'Locative participle: spaCy lemma of "installed" (one L) → located_in', 'core-1 is installed in rack-1',      'seed_locative', 0.85),
  ('mount',    'locative_participle', 'Locative participle: "<X> is mounted in/on <place>" → located_in',   'The unit is mounted in rack-2',      'seed_locative', 0.85),
  ('station',  'locative_participle', 'Locative participle: "<X> is stationed at <place>" → located_in',    'The team is stationed at site-b',    'seed_locative', 0.75),
  ('base',     'locative_participle', 'Locative participle: "<X> is based in <place>" → located_in',        'The lab is based in building-4',     'seed_locative', 0.72),
  ('place',    'locative_participle', 'Locative participle: "<X> is placed in <place>" → located_in',       'The sample is placed in freezer-2',  'seed_locative', 0.70),
  ('site',     'locative_participle', 'Locative participle: "<X> is sited in <place>" → located_in',        'The mast is sited on ridge-a',       'seed_locative', 0.68)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (migration 105 / user_schema.sql). Create it if missing
-- (defensive, same DDL), then seed the new category from public. Mirrors 105/167's fan-out.
-- Idempotent: ON CONFLICT DO NOTHING.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
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

        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category = 'locative_participle'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 172: locative_participle cues seeded into %', _schema;
    END LOOP;
END $$;
