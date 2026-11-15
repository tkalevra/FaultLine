-- Migration 167: linguistic_cues — seed the RELOCATION / change-of-residence verb cue class onto the
-- SAME rail (category='relocation_verb').
-- Date: 2026-07-11
--
-- WHY
-- ---
-- The present-tense "I live in Toronto" captures a residence edge (the SVO predicate "live_in" folds
-- onto the seeded ``lives_in`` rel), but the two OTHER surface shapes of the SAME residence fact
-- captured NOTHING on the spine:
--   • "I moved to Tokyo"        — SVO folded a NOVEL ``move_to`` predicate with no residence semantics
--                                 → dropped; only a stray ``tokyo`` entity survived.
--   • "I used to live in London" — the past-habitual "used to" makes ``live`` an xcomp of the modal-
--                                 like ``used`` so the SVO/residence chains never reached it.
-- Both are the marquee STATE-CHANGE ("moved cities") the temporal model exists for (London→Tokyo →
-- "where do I live" must return Tokyo current, London past). A new deterministic deriver chain
-- ``linguistics.derive_sentence_facts._chain_relocation`` recognizes:
--   (A) a RELOCATION verb governing a destination "to"/"into" place  → lives_in @ current, and
--   (B) the past-habitual "used to live/move in/to <place>"          → lives_in @ temporal_status='past'
-- emitting the SAME ``lives_in`` residence rel the present-tense path produces (state changes COEXIST —
-- lives_in is a ``state`` rel, so the old residence is kept as history, recency/temporal picks current).
--
-- The RELOCATION-verb class is resolved from ``<tenant>.linguistic_cues`` (category='relocation_verb')
-- via the SAME per-tenant overlay the naming/lvc/employment/temporal layers use — DB-HELD + per-tenant
-- + GROWABLE, NOT a frozen in-code list. The in-code ``linguistics._RELOCATION_VERB_LEMMAS`` /
-- ``linguistic_cue_overlay._BOOTSTRAP_RELOCATION_VERBS`` frozensets REMAIN only as the DB-DOWN
-- code-fallback seed (fail-safe; never lose detection on a pre-migration / unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (nsubj/prep/pobj/xcomp/
-- aux), the PERSON-subject morphology test (1st-person pronoun or a PROPN name — lives_in
-- head_types=Person), the PLACE-destination gate (a GLiNER2 Location/GPE ent OR a PROPN), and the
-- "used to" past-habitual grammatical shape. Only the RELOCATION-VERB lemma vocabulary is DB data that
-- grows. This is a ⚠️ FLAGGED BOUNDED LEXICAL CLASS (like naming/acquisition/possession/employment): a
-- relocation reading cannot be made purely structural ("move to Tokyo" = residence vs "move the box to
-- the shelf"), so the verb cue class + the parse gate is the discriminator.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL non-carved public.linguistic_cues
-- categories into every NEW tenant — so new tenants inherit this class automatically. This migration
-- only (1) seeds the new category into public and (2) fans it out to EXISTING tenant schemas.
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the relocation class
-- ============================================================================
-- `cue` is matched against the spaCy token lemma (verbs), lowercase. Membership is corroborated
-- downstream by the parse (PERSON subject + a "to"/"into" destination whose pobj is a PLACE).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('move',      'relocation_verb', 'Change-of-residence verb: subject relocates to a destination place', 'I moved to Tokyo',          'seed_relocation', 0.88),
  ('relocate',  'relocation_verb', 'Change-of-residence verb (formal): subject relocates to a place',     'She relocated to Berlin',   'seed_relocation', 0.90),
  ('resettle',  'relocation_verb', 'Change-of-residence verb: subject resettles in a new place',          'We resettled in Halifax',   'seed_relocation', 0.82),
  ('emigrate',  'relocation_verb', 'Change-of-residence verb: subject emigrates to a country',            'He emigrated to Canada',    'seed_relocation', 0.85),
  ('immigrate', 'relocation_verb', 'Change-of-residence verb: subject immigrates to a country',           'They immigrated to Canada', 'seed_relocation', 0.85),
  ('migrate',   'relocation_verb', 'Change-of-residence verb: subject migrates to a place',               'I migrated to Australia',   'seed_relocation', 0.75)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (migration 105 / user_schema.sql). Create it if missing
-- (defensive, same DDL), then seed the new category from public. Mirrors 105/108/112's fan-out.
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
            WHERE category = 'relocation_verb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 167: relocation_verb cues seeded into %', _schema;
    END LOOP;
END $$;
