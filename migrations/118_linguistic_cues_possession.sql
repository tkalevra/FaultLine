-- Migration 118: linguistic_cues — seed the STATIVE POSSESSION verb cue class.
-- Date: 2026-06-27
--
-- WHY
-- ---
-- The named-instance self-possession gate `_type_is_self_possessed` (src/extraction/linguistics.py)
-- decides whether a named instance's TYPE belongs to the speaker, gating the possession edge minted by
-- `_chain_named_instance` ("I have a dog named Rex" → (user, has_pet, rex)). Clause (b) climbed to the
-- governing verb and tested a SINGLE HARDCODED VERB LITERAL `h.lemma_ == "have"`. That `=="have"` box
-- only fits have-shaped sentences: "I OWN a motorcycle named Bolt", "I POSSESS a painting named Dawn",
-- "I KEEP a hamster named Nibbles" all climbed to a NON-have verb → self_possessed=False → the
-- possession edge DROPPED (only the instance_of classification landed). A verb literal is a box that
-- silently drops every construction outside it — the inverse of subject-/linguistic-agnosticism.
--
-- THE FIX
-- -------
-- The clause keeps doing the GRAMMAR (climb to the governing verb via the parse; 1st-person-personal-
-- pronoun subject check) — that is the agnostic structural part and is UNCHANGED. Only the "is this a
-- possession verb" DECISION moves to GROWABLE METADATA: a new STATIVE-POSSESSION verb cue class read
-- via the linguistic_cue_overlay (the SAME seed∪tenant per-tenant overlay machinery as naming_verb /
-- acquisition_verb / kinship_noun). ZERO verb literals remain in `_type_is_self_possessed`.
--
-- DISTINCT FROM acquisition_verb (migration 115): acquisition_verb is the CHANGE-of-possession class
-- (COMING to possess — got/bought/acquired/received). THIS class is STATIVE possession (CURRENTLY
-- possessing — have/own/possess/keep/hold). The named-instance self-possession gate is about a
-- STANDING possession relation ("I have/own a dog named Rex"), so it needs the stative class. `have`
-- is INCLUDED here so the existing family/pet self-possession path keeps working — now AS METADATA,
-- not as an in-code literal.
--
-- ⚠️ FLAGGED BOUNDED LEXICAL CLASS, honestly documented. Like the acquisition class, the possession
-- signal cannot be made purely structural: "I have a dog" (stative possession) and "I have a meeting"
-- (light-verb occurrence) share the SAME dep shape (verb→dobj). Only the verb's lexical semantics
-- distinguishes a possession reading. It is firewalled downstream by the parse the SAME way (the
-- clause climbs only to a 1st-person-personal-pronoun-subject governing verb, and the named-instance
-- binding already requires a ProperName↔Type binding under that verb), and it is DB-HELD + per-tenant
-- + GROWABLE (category='possession_verb') so a tenant grows its own stative possession verbs freq-
-- gated without code edits. The in-code `linguistic_cue_overlay._BOOTSTRAP_POSSESSION_VERBS` frozenset
-- REMAINS only as the DB-DOWN code-fallback seed (fail-safe; never lose detection on a pre-migration /
-- unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the head-climb to the governing verb, the
-- 1st-person possessive-determiner morphology read (clause (a)), the 1st-person-personal-pronoun
-- subject test (`_is_first_person_personal_pronoun`), and the VERB/AUX POS guard. ONLY the
-- POSSESSION-VERB lemma recognition is DB data that grows.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit this class automatically. This migration only (1) seeds
-- the new category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/115).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the possession class
-- ============================================================================
-- `cue` is matched against the spaCy token lemma (verbs), lowercase. Membership is corroborated
-- downstream by the parse (the named-instance self-possession gate: a 1st-person-personal-pronoun
-- subject governing the type noun + a ProperName↔Type binding).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('have',    'possession_verb', 'Stative possession verb: subject currently possesses the object', 'I have a dog named Rex', 'seed_possession', 0.85),
  ('own',     'possession_verb', 'Stative possession verb (ownership)', 'I own a motorcycle named Bolt',                       'seed_possession', 0.92),
  ('possess', 'possession_verb', 'Stative possession verb (formal possess)', 'I possess a painting named Dawn',               'seed_possession', 0.90),
  ('keep',    'possession_verb', 'Stative possession verb (keep/maintain a possession)', 'I keep a hamster named Nibbles',     'seed_possession', 0.78),
  ('hold',    'possession_verb', 'Stative possession verb (hold a possession)', 'I hold a property named Maple Lodge',        'seed_possession', 0.70)
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
            WHERE category = 'possession_verb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 118: possession_verb cues seeded into %', _schema;
    END LOOP;
END $$;
