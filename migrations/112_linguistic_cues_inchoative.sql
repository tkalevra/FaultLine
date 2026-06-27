-- Migration 112: linguistic_cues — seed the INCHOATIVE / ingressive verb cue class onto the SAME rail
-- Date: 2026-06-22
--
-- WHY
-- ---
-- LongMemEval q7 ("Which seeds were started first, the tomatoes or the marigolds?") was un-answerable
-- because the dated START events were never captured: "I started some marigold seeds … on March 3rd"
-- and "I have been starting seeds … since February 20th" carry a real event_date, but the occurrence
-- seam (`analyze_event`) only fires on the LIGHT/SUPPORT-verb class (a support verb governing an
-- EVENTIVE NOUN — "had a visit"). In an INCHOATIVE construction the EVENT meaning is on the VERB
-- itself (ingressive lexical aspect — "start"/"begin" marks the BEGINNING of an activity) and the
-- direct object is the ITEM being started, so analyze_event missed it and the dated start was dropped.
--
-- A new deterministic lane `linguistics.analyze_inchoative` recognizes the ingressive-verb +
-- concrete-direct-object shape and emits the SAME (user, participated_in, <item>) + event_date
-- backbone the residue classifier emits for an ACTION-on-a-thing ("washed my car"), but with NO LLM.
-- The aspectual verb class is resolved from `<tenant>.linguistic_cues` (category='inchoative_verb')
-- via the SAME per-tenant overlay the naming-verb / lvc_support_verb / svo_particle / temporal layers
-- use, so it is DB-HELD + per-tenant + GROWABLE — NOT a frozen in-code list. The in-code
-- `linguistics._INCHOATIVE_VERB_LEMMAS` / `linguistic_cue_overlay._BOOTSTRAP_INCHOATIVE_VERBS`
-- frozensets REMAIN only as the DB-DOWN code-fallback seed (fail-safe; never lose detection on a
-- pre-migration / unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (dobj/obj/xcomp/
-- compound/amod), the POS guards (object must be a NOUN → "started crying"/"started to think" with a
-- VERB complement never fire), the 1st-person-subject morphology test, and the neg-dependency read.
-- Only the INGRESSIVE-VERB lemma recognition is DB data that grows.
--
-- This is a LEXICAL-ASPECT grammatical class (the ingressive verbs), corroborated downstream by the
-- parse (a concrete direct object + a clause date) — NOT a domain/event word-list. A non-eventive use
-- ("I started to think") is rejected grammatically, so growing the set never over-captures.
--
-- NO DDL CHANGE: migration 105 already created the table (public + per-tenant) general-by-category and
-- the provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories
-- into every NEW tenant — so new tenants inherit this class automatically. This migration only
-- (1) seeds the new category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the inchoative class
-- ============================================================================
-- `cue` is matched against the spaCy token lemma (verbs), lowercase. Membership is corroborated
-- downstream by the parse (1st-person subject + a concrete NOUN direct object + a clause date).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('start',     'inchoative_verb', 'Ingressive verb marking the beginning of an activity (transitive)', 'I started the seeds on March 3rd', 'seed_inchoative', 0.90),
  ('begin',     'inchoative_verb', 'Ingressive verb marking the beginning of an activity', 'I began piano lessons in January',          'seed_inchoative', 0.90),
  ('commence',  'inchoative_verb', 'Ingressive verb (formal) marking the beginning of an activity', 'I commenced the program in March',    'seed_inchoative', 0.82),
  ('launch',    'inchoative_verb', 'Ingressive verb marking the initiation of a project/effort', 'I launched the project in April',         'seed_inchoative', 0.80),
  ('initiate',  'inchoative_verb', 'Ingressive verb marking the initiation of a process', 'I initiated the upgrade last week',             'seed_inchoative', 0.80),
  ('undertake', 'inchoative_verb', 'Ingressive verb marking the start of an undertaking', 'I undertook the renovation in May',             'seed_inchoative', 0.75)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (migration 105 / user_schema.sql). Create it if missing
-- (defensive, same DDL), then seed the new category from public. Mirrors 105/108's fan-out.
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
            WHERE category = 'inchoative_verb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 112: inchoative_verb cues seeded into %', _schema;
    END LOOP;
END $$;
