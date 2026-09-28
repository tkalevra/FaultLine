-- Migration 189: linguistic_cues — seed the POSITION-NOUN (job/role container head) cue class.
-- Date: 2026-07-18
--
-- WHY
-- ---
-- An occupation is very often stated NOT with an employment verb but with a POSSESSED POSITION NOUN
-- whose apposed "as <NP>" carries the role — "I've used Trello in my previous ROLE as a marketing
-- specialist at a small startup", "her new POSITION as CTO", "his JOB as a nurse at the clinic". The
-- sentence's MAIN verb is unrelated ("use"/"be"), so the existing `employment_verb` construction
-- (which keys on the VERB) never fires and NOTHING occupation-shaped is captured — the role noun
-- falls to a junk `(user, owns, role)` or is dropped. So "What was my previous occupation?" recalls
-- empty (LME 5d3d2817).
--
-- This cue class lets the deriver recognize the POSSESSED-POSITION-NOUN frame and reuse the SAME
-- occupation + works_for emission the employment "as <role> [at|for <org>]" construction already
-- produces (linguistics.derive_sentence_facts) — the noun-headed twin of `employment_verb`. The
-- SUBJECT is the possessor (1st-person "my"/"our" → user; a named genitive "Sarah's role" → that
-- name), the occupation is the "as" pobj, and a nested "at|for <ORG>" → works_for.
--
-- ⚠️ FLAGGED BOUNDED LEXICAL CLASS (honestly documented, EXACTLY like the naming / employment_verb /
-- problem_noun classes already on this rail). The position reading cannot be made purely structural:
-- "my ROLE as X" (occupation) and "my HOUSE as X" (collateral) share the SAME dep shape (possessed
-- NOUN → apposed "as" NP). Only the head noun's lexical semantics distinguishes a position container
-- from an arbitrary possessed thing, so a small bounded NOUN class is unavoidable — it is the SAFETY
-- GATE: a possessed noun NOT in this class never mints an occupation. It is firewalled downstream by
-- the parse the SAME way the others are (a possessive `poss` child + an apposed "as <NP>"). DB-HELD +
-- per-tenant + GROWABLE (category='position_noun') so a tenant grows its own position heads
-- (freq-gated) without code edits. The in-code `linguistic_cue_overlay._BOOTSTRAP_POSITION_NOUNS`
-- frozenset REMAINS only as the DB-DOWN code-fallback seed (fail-safe; never lose detection on a
-- pre-migration / unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (poss child, prep(as)/
-- pobj for the role, prep(at|for)/pobj for the org), the POS guards, the 1st-person-possessor
-- morphology test. Only the POSITION-NOUN lemma recognition is DB data that grows.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit this class automatically. This migration only (1) seeds
-- the new category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/116).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the position-noun class
-- ============================================================================
-- `cue` is matched against the spaCy head-noun lemma (lowercase). Membership is corroborated
-- downstream by the parse (a possessive `poss` child + an apposed "as <NP>" naming the occupation).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('role',        'position_noun', 'Generic position/appointment container; apposed "as <NP>" carries the occupation', 'my previous role as a marketing specialist', 'seed_position_noun', 0.90),
  ('position',    'position_noun', 'Generic position/appointment container head',                                       'her new position as CTO',                    'seed_position_noun', 0.90),
  ('job',         'position_noun', 'Generic position/appointment container head',                                       'his job as a nurse at the clinic',           'seed_position_noun', 0.88),
  ('title',       'position_noun', 'Generic position/appointment container head',                                       'my title as lead engineer',                  'seed_position_noun', 0.80),
  ('post',        'position_noun', 'Generic position/appointment container head',                                       'her post as ambassador',                     'seed_position_noun', 0.78),
  ('capacity',    'position_noun', 'Generic position/appointment container head',                                       'in my capacity as treasurer',                'seed_position_noun', 0.76),
  ('appointment', 'position_noun', 'Generic position/appointment container head',                                       'his appointment as director',                'seed_position_noun', 0.74),
  ('gig',         'position_noun', 'Generic position/appointment container head (informal)',                            'my gig as a barista',                        'seed_position_noun', 0.70),
  ('stint',       'position_noun', 'Generic position/appointment container head',                                       'my stint as a consultant',                   'seed_position_noun', 0.70),
  ('tenure',      'position_noun', 'Generic position/appointment container head',                                       'her tenure as chair',                        'seed_position_noun', 0.70),
  ('function',    'position_noun', 'Generic position/appointment container head',                                       'my function as coordinator',                 'seed_position_noun', 0.66)
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
            WHERE category = 'position_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 189: position_noun cues seeded into %', _schema;
    END LOOP;
END $$;
