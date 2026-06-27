-- Migration 108: linguistic_cues — seed two MORE verb/particle cue classes onto the SAME rail
-- Date: 2026-06-20
--
-- WHY
-- ---
-- Migration 105 created public.linguistic_cues as a GENERAL-by-`category` table and seeded only the
-- `naming_verb` class, retiring the in-code `_NAMING_VERB_LEMMAS` frozenset to a DB-down fallback
-- seed. Two MORE frozen in-code lexical lists in src/extraction/linguistics.py are the same "frozen
-- list pretending to be a rule" brittleness and are now converted to the identical DB-held +
-- per-tenant + overlay-resolved + growable contract:
--
--   * `_LVC_SUPPORT_VERB_LEMMAS` (the light/support-verb class in analyze_event / analyze_svo_relations:
--     have/go/attend/take/do/make/get/participate) → category 'lvc_support_verb'
--   * `_SVO_KEEP_PARTICLES` (the load-bearing particle/preposition class in _svo_predicate_token /
--     _svo_object_head: to/for/with/in/on/at/from/into/about/of) → category 'svo_particle'
--
-- Both in-code frozensets REMAIN in linguistics.py as the DB-DOWN CODE-FALLBACK SEED (resolved via
-- linguistic_cue_overlay.resolve_lvc_support_verbs / resolve_svo_particles — the SAME ContextVar-
-- bound per-tenant overlay the naming-verb / rel_type / taxonomy / temporal layers use). Membership
-- checks now call the resolver, never the frozenset directly. Detection is NEVER lost on DB-down /
-- pre-migration / unwarmed-overlay: the resolver fails safe to the frozenset.
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (dobj/pobj/prep/prt/
-- attr/oprd/compound/amod …) and the universal POS function-word tag set (_FUNCTION_POS). Only the
-- VERB-LEMMA / PARTICLE-SURFACE recognition becomes DB data that grows.
--
-- NO DDL CHANGE: migration 105 already created the table (public + per-tenant) general-by-category
-- and the provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues
-- categories into every NEW tenant — so new tenants inherit these classes automatically. This
-- migration only (1) seeds the two new categories into public and (2) fans them out to EXISTING
-- tenant schemas. Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- Guard: if migration 105 has not run, create the table in public so this seed has a target. This is
-- the SAME DDL as 105 (idempotent), so applying 108 before 105 is harmless; 105's per-tenant fan-out
-- still runs later.
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the two new classes
-- ============================================================================
-- `cue` is matched against the spaCy token lemma (verbs) or surface form (particles), lowercase.

-- (a) LVC light/support-verb class. A lexical-aspect grammatical class (the light verbs that form a
--     light-verb construction by governing an eventive complement) — the evidenced retired in-code
--     seed. Membership is corroborated downstream by the parse (eventive noun as the governed object).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('have',        'lvc_support_verb', 'Light/support verb governing an eventive object', 'I had a meeting',          'seed_lvc_support', 0.90),
  ('go',          'lvc_support_verb', 'Motion support verb (governed-prep eventive object)', 'I went to a concert',   'seed_lvc_support', 0.88),
  ('attend',      'lvc_support_verb', 'Attendance support verb', 'I attended a workshop',                             'seed_lvc_support', 0.90),
  ('take',        'lvc_support_verb', 'Light verb governing an eventive object', 'I took a trip',                     'seed_lvc_support', 0.85),
  ('do',          'lvc_support_verb', 'Light verb governing an eventive object', 'I did an interview',                'seed_lvc_support', 0.82),
  ('make',        'lvc_support_verb', 'Light verb governing an eventive object', 'I made a visit',                    'seed_lvc_support', 0.82),
  ('get',         'lvc_support_verb', 'Light verb governing an eventive object', 'I got a checkup',                   'seed_lvc_support', 0.78),
  ('participate', 'lvc_support_verb', 'Participation support verb (governed-prep eventive object)', 'I participated in a webinar', 'seed_lvc_support', 0.88)
ON CONFLICT (cue, category) DO NOTHING;

-- (b) Load-bearing SVO-particle class. The closed grammatical class of particles/prepositions that
--     change the verb's relation when governed by it ("go" vs "go to", "work" vs "work for"). A
--     language primitive (the ADP/PART surface forms), aligned with predicate_span._KEEP_PREPOSITIONS.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('to',    'svo_particle', 'Load-bearing particle/prep on a verb', 'went to a concert',  'seed_svo_particle', 0.90),
  ('for',   'svo_particle', 'Load-bearing particle/prep on a verb', 'works for Acme',     'seed_svo_particle', 0.90),
  ('with',  'svo_particle', 'Load-bearing particle/prep on a verb', 'met with the team',  'seed_svo_particle', 0.85),
  ('in',    'svo_particle', 'Load-bearing particle/prep on a verb', 'participated in it',  'seed_svo_particle', 0.85),
  ('on',    'svo_particle', 'Load-bearing particle/prep on a verb', 'worked on the plan', 'seed_svo_particle', 0.82),
  ('at',    'svo_particle', 'Load-bearing particle/prep on a verb', 'looked at it',       'seed_svo_particle', 0.80),
  ('from',  'svo_particle', 'Load-bearing particle/prep on a verb', 'moved from there',   'seed_svo_particle', 0.82),
  ('into',  'svo_particle', 'Load-bearing particle/prep on a verb', 'moved into a house', 'seed_svo_particle', 0.82),
  ('about', 'svo_particle', 'Load-bearing particle/prep on a verb', 'asked about it',     'seed_svo_particle', 0.78),
  ('of',    'svo_particle', 'Load-bearing particle/prep on a verb', 'thought of it',      'seed_svo_particle', 0.75)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (migration 105 / user_schema.sql). Create it if missing
-- (defensive, same DDL — handles a tenant provisioned before 105), then seed the TWO NEW categories
-- from public. Mirrors 105's fan-out. Idempotent: ON CONFLICT DO NOTHING.

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

        -- ---- seed the TWO NEW categories from public (template) ----
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category IN ('lvc_support_verb', 'svo_particle')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 108: lvc_support_verb + svo_particle cues seeded into %', _schema;
    END LOOP;
END $$;
