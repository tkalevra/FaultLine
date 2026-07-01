-- Migration 125: linguistic_cues — seed the EMPLOYMENT / ROLE-PREDICATION verb cue class.
-- Date: 2026-07-01
--
-- WHY
-- ---
-- The spine deriver (src/extraction/linguistics.py `derive_sentence_facts`) had NO chain for the
-- EMPLOYMENT construction "<subject> <employment verb> as <role> [at|for <org>]". So
-- "I work as a Systems Analyst III at the University of Springfield's Computing Services" fell to the
-- LLM relation-fill / rewrite, which DROPPED the role ("Systems Analyst III") and MISLABELED
-- "work at <university>" as `educated_at` (a university object biases the weak 9B model to education),
-- plus leaked genitive `related_to` junk. The existing occupation seam only handles the COPULA
-- ("I am a Systems Analyst" → occupation), not "I WORK AS a …".
--
-- THE FIX
-- -------
-- A new deterministic chain `_chain_employment` does the GRAMMAR (a grammatical subject — 1st-person
-- personal pronoun OR a named 3rd-person subject — governing the verb; the `as`/`at`/`for` PP frame;
-- the full role-title span) — that is the agnostic structural part. Only the "is this an employment
-- verb" DECISION is GROWABLE METADATA: this new cue class read via the linguistic_cue_overlay (the
-- SAME seed∪tenant per-tenant overlay machinery as naming_verb / acquisition_verb / possession_verb /
-- kinship_noun). ZERO employer/role/subject literals in the chain.
--
-- The cue class IS the SAFETY GATE that lets the chain be BROAD without over-capturing: "I DRESSED as
-- a pirate" / "he is KNOWN as Ace" — `dress`/`know` are NOT employment verbs → NEVER read as an
-- occupation. And `study`/`graduate` are NOT in the class → "I studied at the University" still routes
-- to `educated_at`, untouched.
--
-- ⚠️ FLAGGED BOUNDED LEXICAL CLASS, honestly documented — like naming / acquisition / possession, the
-- employment "as <role>" reading cannot be made purely structural: "work as a nurse" (role) and "dress
-- as a pirate" (costume) share the SAME prep-`as` dep shape; only the verb's lexical semantics
-- distinguishes an employment/role-holding reading. It is firewalled downstream by the parse the SAME
-- way (a grammatical subject + the as/at/for PP frame), and it is DB-HELD + per-tenant + GROWABLE
-- (category='employment_verb') so a tenant grows its own employment verbs freq-gated without code
-- edits. The in-code `linguistic_cue_overlay._BOOTSTRAP_EMPLOYMENT_VERBS` /
-- `linguistics._EMPLOYMENT_VERB_LEMMAS` frozensets REMAIN only as the DB-DOWN code-fallback seed
-- (fail-safe; never lose detection on a pre-migration / unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the grammatical subject resolution
-- (`_is_first_person_personal_pronoun` / named subject), the as/at/for PP frame walk, the full-title
-- span build, and the temporal/duration-"for" exclusion. ONLY the EMPLOYMENT-VERB lemma recognition is
-- DB data that grows.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit this class automatically. This migration only (1) seeds
-- the new category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/118).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the employment class
-- ============================================================================
-- `cue` is matched against the spaCy token lemma (verbs), lowercase. Membership is corroborated
-- downstream by the parse (a grammatical subject governing the verb + an as/at/for PP frame).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('work',     'employment_verb', 'Employment / role-predication verb: subject works as a role / at an employer', 'I work as a nurse at the clinic',  'seed_employment', 0.92),
  ('serve',    'employment_verb', 'Employment / role-predication verb (serve as a role)',                          'she serves as treasurer',          'seed_employment', 0.82),
  ('act',      'employment_verb', 'Employment / role-predication verb (act as a role)',                            'he acts as mediator',              'seed_employment', 0.72),
  ('function', 'employment_verb', 'Employment / role-predication verb (function as a role)',                       'I function as the lead',           'seed_employment', 0.68),
  ('employ',   'employment_verb', 'Employment / role-predication verb (employed as a role)',                       'I am employed as an engineer',     'seed_employment', 0.90),
  ('hire',     'employment_verb', 'Employment / role-predication verb (hired as a role)',                          'she was hired as a manager',       'seed_employment', 0.88),
  ('appoint',  'employment_verb', 'Employment / role-predication verb (appointed as a role)',                      'he was appointed as chair',        'seed_employment', 0.85),
  ('contract', 'employment_verb', 'Employment / role-predication verb (contracted as a role)',                     'I was contracted as a consultant', 'seed_employment', 0.66)
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
            WHERE category = 'employment_verb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 125: employment_verb cues seeded into %', _schema;
    END LOOP;
END $$;
