-- Migration 308: linguistic_cues — seed the PET-NOUN head cue class.
-- Date: 2026-11-07
--
-- WHY
-- ---
-- Fog-family lab F5 (bench seat cd0c09ed, DEV/FINDINGS-fog-family-lab-20261107.md): the turn
-- "We have a cat named Turnip." committed ONE fact and NO has_pet row — the always-on
-- object-named-value seam (linguistics._chain_object_named_value) filed only the alias-only edge
-- ((cat, also_known_as, Turnip)) while the raw SVO have-frame carries a rel the seed does not
-- define, so the gate dropped it. Every pets query then starved and the doc chunk filled the void.
--
-- THE FIX
-- -------
-- A new PET-NOUN head cue class read via linguistic_cue_overlay.resolve_pet_nouns (the SAME
-- seed-union-tenant per-tenant overlay machinery as possession_verb / employment_verb /
-- measure_noun). The head noun's PET-NESS is lexical semantics (a dog vs a dollar are both count
-- nouns) and cannot be made purely structural, so — like every class here — it is DB-HELD +
-- per-tenant + GROWABLE (freq-gated growth, no code edits). The in-code _BOOTSTRAP_PET_NOUNS
-- frozenset (linguistic_cue_overlay.py) remains only as the DB-DOWN code-fallback floor.
--
-- Multi-word heads ride as PHRASE cues ("guinea pig") — the consumer matches the full type NP as
-- well as the head, so a compound head ("guinea pig", head noun "pig") still resolves WITHOUT
-- seeding the risky bare "pig".
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and
-- the provisioning seeder blanket-copies ALL public.linguistic_cues categories into every NEW
-- tenant — new tenants inherit automatically. This migration only (1) seeds the new category into
-- public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/118).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the pet-noun class
-- ============================================================================
-- 'cue' is matched against the head-noun lemma/surface (or the full type NP for phrases),
-- lowercase. Membership is corroborated downstream by the parse (the self-possession gate
-- _type_is_self_possessed + the ProperName-name-Type binding), so a stray noun can never mint a
-- pet edge on its own. Mirrors _BOOTSTRAP_PET_NOUNS exactly.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('pet',        'pet_noun', 'Companion-animal head noun (generic)', 'We have a pet named Turnip',      'seed_pet_noun', 0.95),
  ('cat',        'pet_noun', 'Companion-animal head noun',           'We have a cat named Turnip',      'seed_pet_noun', 0.92),
  ('kitten',     'pet_noun', 'Companion-animal head noun',           'We have a kitten named Miso',     'seed_pet_noun', 0.90),
  ('dog',        'pet_noun', 'Companion-animal head noun',           'I have a dog named Rex',          'seed_pet_noun', 0.92),
  ('puppy',      'pet_noun', 'Companion-animal head noun',           'We adopted a puppy named Clover', 'seed_pet_noun', 0.90),
  ('bird',       'pet_noun', 'Companion-animal head noun',           'We have a bird named Kiwi',       'seed_pet_noun', 0.85),
  ('rabbit',     'pet_noun', 'Companion-animal head noun',           'We have a rabbit named Clover',   'seed_pet_noun', 0.90),
  ('bunny',      'pet_noun', 'Companion-animal head noun',           'Our bunny named Thumper',         'seed_pet_noun', 0.85),
  ('hamster',    'pet_noun', 'Companion-animal head noun',           'I have a hamster named Nibbles',  'seed_pet_noun', 0.90),
  ('gerbil',     'pet_noun', 'Companion-animal head noun',           'We have a gerbil named Pip',      'seed_pet_noun', 0.85),
  ('fish',       'pet_noun', 'Companion-animal head noun',           'We have a fish named Bubbles',    'seed_pet_noun', 0.85),
  ('goldfish',   'pet_noun', 'Companion-animal head noun',           'Our goldfish named Comet',        'seed_pet_noun', 0.85),
  ('turtle',     'pet_noun', 'Companion-animal head noun',           'We have a turtle named Shelly',   'seed_pet_noun', 0.88),
  ('tortoise',   'pet_noun', 'Companion-animal head noun',           'Our tortoise named Tiptoe',       'seed_pet_noun', 0.85),
  ('lizard',     'pet_noun', 'Companion-animal head noun',           'We have a lizard named Rex',      'seed_pet_noun', 0.82),
  ('gecko',      'pet_noun', 'Companion-animal head noun',           'Our gecko named Mango',           'seed_pet_noun', 0.82),
  ('snake',      'pet_noun', 'Companion-animal head noun',           'I have a snake named Noodle',     'seed_pet_noun', 0.82),
  ('parrot',     'pet_noun', 'Companion-animal head noun',           'We have a parrot named Pepper',   'seed_pet_noun', 0.85),
  ('canary',     'pet_noun', 'Companion-animal head noun',           'Our canary named Sunny',          'seed_pet_noun', 0.82),
  ('budgie',     'pet_noun', 'Companion-animal head noun',           'We have a budgie named Blue',     'seed_pet_noun', 0.82),
  ('ferret',     'pet_noun', 'Companion-animal head noun',           'I have a ferret named Bandit',    'seed_pet_noun', 0.85),
  ('guinea pig', 'pet_noun', 'Companion-animal head noun (PHRASE cue - matched against the full type NP)', 'We have a guinea pig named Truffle', 'seed_pet_noun', 0.90),
  ('mouse',      'pet_noun', 'Companion-animal head noun',           'We have a mouse named Whiskers',  'seed_pet_noun', 0.75),
  ('rat',        'pet_noun', 'Companion-animal head noun',           'She has a rat named Peanut',      'seed_pet_noun', 0.75),
  ('horse',      'pet_noun', 'Companion-animal head noun',           'We have a horse named Duke',      'seed_pet_noun', 0.80),
  ('pony',       'pet_noun', 'Companion-animal head noun',           'Our pony named Buttercup',        'seed_pet_noun', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

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
            WHERE category = 'pet_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 308: pet_noun cues seeded into %', _schema;
    END LOOP;
END $$;
