-- Migration 115: linguistic_cues — seed the ACQUISITION / transfer-of-possession verb cue class.
-- Date: 2026-06-23
--
-- WHY
-- ---
-- Q4 ("Which device did I get first, the Samsung Galaxy S22 or the Dell XPS 13?") was un-answerable
-- because the PRIMARY user→device ownership linkage of an acquisition turn was never EXPOSED as a
-- dated, comparable fact. "I got a Samsung Galaxy S22 at the mall on Feb 20" parses as a light-verb
-- shape (get → dobj PROPN), so the occurrence seam rejected the device dobj as "possession not an
-- event" (the POS rule) and instead PROMOTED the locative prep-pobj ("mall"/"store") to a
-- participated_in occurrence. The device ownership survived only as a grown `get` rel in staged_facts
-- which the comparison/ordinal lane (reads dated facts from `facts`) could not reach.
--
-- A new deterministic lane `linguistics.analyze_acquisition` recognizes the TRANSFER-OF-POSSESSION
-- construction (an acquisition verb + 1st-person subject + a concrete direct object that becomes the
-- subject's possession) and the caller EXPOSES the inferred linkage as `(user, owns, <device>)` —
-- Class B (llm_inferred), carrying the acquisition event_date — PLUS the device's L4 cast
-- (instance_of <thin-type>) and the locative kept as a co-bound LOCATION (located_in), never promoted
-- to the event and never deleted. The device is then the operand-resolvable object the "which device
-- first" comparison grounds + orders by date.
--
-- ⚠️ FLAGGED BOUNDED LEXICAL CLASS (Q4 brief, honestly documented). Unlike the fully-structural seams,
-- the acquisition signal CANNOT be made purely structural: "got a phone" (coming-to-possess) and "had
-- a meeting" (light-verb occurrence) share the SAME dep shape (verb→dobj). Only the verb's lexical
-- semantics distinguishes them, so a small bounded verb class is unavoidable — EXACTLY as for the
-- naming / lvc_support / inchoative / aspectual_control classes already on this rail. It is firewalled
-- downstream by the parse the SAME way (1st-person subject + a concrete possession object; a verb-
-- complement xcomp "I got to leave" or an eventive-noun dobj "I got a haircut" is excluded by POS +
-- the possession-object discipline), and is DB-HELD + per-tenant + GROWABLE (category=
-- 'acquisition_verb') so a tenant grows its own transfer verbs freq-gated without code edits. The
-- in-code `linguistic_cue_overlay._BOOTSTRAP_ACQUISITION_VERBS` frozenset REMAINS only as the DB-DOWN
-- code-fallback seed (fail-safe; never lose detection on a pre-migration / unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (dobj/obj, prep/pobj for
-- the locative, appos for the alias), the POS guards (object must be a NOUN/PROPN possession; a VERB
-- xcomp complement never fires), the 1st-person-subject morphology test, and the neg-dependency read.
-- Only the ACQUISITION-VERB lemma recognition is DB data that grows.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit this class automatically. This migration only (1) seeds
-- the new category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/112).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the acquisition class
-- ============================================================================
-- `cue` is matched against the spaCy token lemma (verbs), lowercase. Membership is corroborated
-- downstream by the parse (1st-person subject + a concrete NOUN/PROPN possession direct object).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('get',      'acquisition_verb', 'Transfer-of-possession verb: subject comes to possess the object', 'I got a Samsung Galaxy S22 on Feb 20', 'seed_acquisition', 0.85),
  ('buy',      'acquisition_verb', 'Transfer-of-possession verb (purchase)', 'I bought a Dell XPS 13 last month',        'seed_acquisition', 0.90),
  ('purchase', 'acquisition_verb', 'Transfer-of-possession verb (formal purchase)', 'I purchased a tablet in March',     'seed_acquisition', 0.90),
  ('acquire',  'acquisition_verb', 'Transfer-of-possession verb (formal acquire)', 'I acquired a new laptop in April',   'seed_acquisition', 0.85),
  ('obtain',   'acquisition_verb', 'Transfer-of-possession verb (obtain)', 'I obtained a licence last week',             'seed_acquisition', 0.78),
  ('receive',  'acquisition_verb', 'Transfer-of-possession verb (receive a gift/item)', 'I received a phone for my birthday', 'seed_acquisition', 0.78),
  ('grab',     'acquisition_verb', 'Informal transfer-of-possession verb', 'I grabbed a coffee maker at the store',      'seed_acquisition', 0.65),
  ('pick',     'acquisition_verb', 'Transfer-of-possession verb (pick up — phrasal)', 'I picked up a monitor yesterday', 'seed_acquisition', 0.65)
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
            WHERE category = 'acquisition_verb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 115: acquisition_verb cues seeded into %', _schema;
    END LOOP;
END $$;
