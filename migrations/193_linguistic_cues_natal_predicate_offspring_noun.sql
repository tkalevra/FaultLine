-- Migration 193: linguistic_cues — NATAL / BIRTH-EVENT cue classes (natal_predicate + offspring_noun).
-- Date: 2026-07-26
--
-- WHY
-- ---
-- A birth is very often stated as "X was BORN", "X had/welcomed a BABY named Y", "X gave birth to
-- twins Ava and Lily". The deriver's new ``_chain_natal_birth`` (FrameNet "Being_born"/"Giving_birth")
-- types the NEWBORN NAMED person ``instance_of`` the birth type "baby" so a "how many babies were
-- born" cardinality (LME qid 2e6d26dc, gold=5) has distinct, countable baby entities to walk — instead
-- of the newborns scattered as instance_of boy/son/girl with no unifying type (the product answered
-- "I don't have any … on record"). The chain reads TWO DB cue classes (subject-agnostic, growable):
--   • natal_predicate (VERBS): the passive birth predicates — "was BORN" (spaCy lemma "bear"),
--     "DELIVERED". Detected morphologically (auxpass) but the LEMMA class is DB data so a tenant grows
--     its own birth verbs freq-gated.
--   • offspring_noun (NOUNS): the offspring / newborn nouns a birth NAME binds to (son/daughter/baby/
--     boy/girl/twin/…). Doubles as a keyed map (SAME rail as kinship_noun): a row whose `description`
--     == 'birth' is a SELF-GATING newborn noun (baby/newborn/infant) whose mere presence marks a birth
--     event. A generic offspring noun (son/boy/twin) only anchors a newborn name INSIDE an already-natal
--     clause (born-verb OR birth-marker present), so "my son likes soccer" is never mis-typed a baby.
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the UD dependency structure (passive nsubjpass +
-- auxpass, appos/conj, naming acl→oprd), the POS guards, and THE HARD LINE (name→naming layer, type→
-- instance_of). Only the verb-/noun-LEMMA vocabulary is DB data that grows. The in-code
-- ``linguistics._NATAL_PREDICATE_LEMMAS`` / ``_OFFSPRING_NOUN_LEMMAS`` / ``_OFFSPRING_BIRTH_MARKER_LEMMAS``
-- frozensets (the DB-DOWN code-fallback seeds) mirror these rows so a pre-migration / unwarmed-overlay
-- turn also detects the frame (fail-safe parity — the whole fix works today on the bootstrap floor).
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit these cues automatically. This migration only (1) seeds the
-- rows into public and (2) fans them out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/191).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the natal cue classes
-- ============================================================================
-- natal_predicate `cue` = spaCy verb lemma (lowercase), matched under an auxpass (passive) context.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('bear',    'natal_predicate', 'Passive birth predicate (spaCy lemma of "born")', 'my son was born in March', 'seed_natal_predicate', 0.9),
  ('deliver', 'natal_predicate', 'Birth/delivery predicate',                        'she delivered a baby girl', 'seed_natal_predicate', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- offspring_noun `cue` = noun lemma the newborn NAME binds to. `description`='birth' marks the
-- SELF-GATING newborn nouns (baby/newborn/infant) whose presence alone marks a birth event.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('baby',           'offspring_noun', 'birth', 'they had a baby named Jasper',           'seed_offspring_noun', 0.9),
  ('newborn',        'offspring_noun', 'birth', 'their newborn, a girl named Ava',        'seed_offspring_noun', 0.9),
  ('infant',         'offspring_noun', 'birth', 'the infant was named Max',               'seed_offspring_noun', 0.9),
  ('son',            'offspring_noun', NULL,    'her son Max was born in March',          'seed_offspring_noun', 0.85),
  ('daughter',       'offspring_noun', NULL,    'their daughter Charlotte was born',      'seed_offspring_noun', 0.85),
  ('child',          'offspring_noun', NULL,    'their third child, a boy named Jasper',  'seed_offspring_noun', 0.85),
  ('kid',            'offspring_noun', NULL,    'their new kid',                          'seed_offspring_noun', 0.7),
  ('boy',            'offspring_noun', NULL,    'a baby boy named Jasper',                'seed_offspring_noun', 0.8),
  ('girl',           'offspring_noun', NULL,    'a baby girl named Charlotte',            'seed_offspring_noun', 0.8),
  ('twin',           'offspring_noun', NULL,    'twins Ava and Lily were born',           'seed_offspring_noun', 0.8),
  ('triplet',        'offspring_noun', NULL,    'triplets were born',                     'seed_offspring_noun', 0.75),
  ('grandson',       'offspring_noun', NULL,    'a grandson named Leo was born',          'seed_offspring_noun', 0.8),
  ('granddaughter',  'offspring_noun', NULL,    'a granddaughter named Mia was born',     'seed_offspring_noun', 0.8),
  ('grandchild',     'offspring_noun', NULL,    'their grandchild was born',              'seed_offspring_noun', 0.75),
  ('nephew',         'offspring_noun', NULL,    'a nephew named Sam was born',            'seed_offspring_noun', 0.75),
  ('niece',          'offspring_noun', NULL,    'a niece named Zoe was born',             'seed_offspring_noun', 0.75)
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
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category IN ('natal_predicate', 'offspring_noun')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 193: natal_predicate/offspring_noun cues seeded into %', _schema;
    END LOOP;
END $$;
