-- Migration 196: linguistic_cues — two NEW cue CATEGORIES for the spine deriver.
--   (a) implicative_verb        — PAST-TENSE actuality-entailing control matrices ("had to", "got to")
--   (b) exemplification_marker  — Hearst lexico-syntactic hyponymy markers ("such as", "including",
--                                 "like"), carrying a POLYSEMY MODE in `description`
-- Date: 2026-07-30
--
-- WHY (a) implicative_verb  (seeds the cue class the CODE fix in commit 31d90d7f already reads)
-- ------------------------
-- `linguistics._aspectual_activity_xcomp` rejects ANY xcomp carrying an infinitival `to` marker, on
-- the (correct, for ITS class) grounds that "want TO buy" / "plan TO visit" is UNREALIZED INTENT.
-- That silently killed the whole clause for an IMPLICATIVE matrix. Measured 2026-07-30:
--     "I had to take my stand mixer to a repair shop last month."  -> only (user, owns, stand mixer);
--        the take-event was never emitted and the resolved 2022-02-05 was reported ORPHANED.
--     "I took my stand mixer to a repair shop last month."          -> the edge landed fine.
--     "I had to buy a new coffee maker last month."                 -> NO FACTS AT ALL.
-- Karttunen 1971, "Implicative Verbs", Language 47(2):340-358 defines the implicative class by
-- exactly this entailment: the affirmative matrix ENTAILS its complement was realized, unlike the
-- non-implicative intent verbs. For the modal-necessity member ("had to") the realis reading is the
-- ACTUALITY ENTAILMENT that perfective/past aspect forces on a root modal — Bhatt 1999, "Covert
-- Modality in Non-Finite Contexts" (ch. 5); Hacquard 2006, "Aspects of Modality".
--
-- ⚠️ POSITIVE (affirmative-entailing) IMPLICATIVES ONLY. Karttunen's NEGATIVE implicatives —
-- forget / neglect / fail / decline / refuse / avoid — entail the complement did NOT happen; seeding
-- one would mint a fact for an event the user explicitly said never occurred. They are deliberately
-- absent, and the CALL SITE additionally applies an ABSOLUTE firewall (matrix must not be in
-- `predicate_span._CATENATIVE` / `_MENTAL_STATE`) which holds even for a GROWN row — so a tenant that
-- grows "want" onto this rail still cannot produce an irrealis fact. Consequence, deliberate:
-- `manage` / `happen` / `need` / `decide` sit in `_CATENATIVE` and stay blocked even though `manage`
-- and `happen` are genuine implicatives; moving them is a separate, owner-visible decision.
--
-- WHY (b) exemplification_marker
-- ------------------------------
-- Hearst 1992, "Automatic Acquisition of Hyponyms from Large Text Corpora", COLING-92 §2: English has
-- a small set of lexico-syntactic patterns ("NP0 such as NP1", "NP0 including NP1", "NP0, like NP1")
-- whose entire job is to ASSERT hyponymy — precisely the L4 PLACE (`instance_of`) edge this engine is
-- built on. They were being lost, and two of them DESTRUCTIVELY. Measured 2026-07-30:
--     "…attending various workshops and lectures, LIKE the workshop on 'Effective Time Management'
--      at the local community center last Saturday."  -> the NAMED event VANISHED entirely.
--     "I have several pets, SUCH AS a dog named Rex."   -> object "several pets AS dog"  (garbage)
--     "I visited many museums INCLUDING the Louvre last April."
--                                                          -> object "many museums INCLUDING louvre"
--                                                             + the resolved 2022-04-05 ORPHANED
--
-- The `description` column carries the POLYSEMY MODE (the same set+map-on-one-rail shape
-- `identifier_noun` uses for its 'strong'/'suffix' role):
--     'unambiguous'    — no non-exemplifying reading in this dep shape ("such as", "including")
--     'comma_required' — POLYSEMOUS; only reads as exemplification under nonrestrictive comma
--                        apposition. "like" is the case: "workshops and lectures, LIKE the workshop
--                        on X" exemplifies, but "I ate lunch LIKE a king" / "I treat my dogs LIKE
--                        children" is a MANNER adjunct and must mint NOTHING.
-- Only markers that surface as a `prep` governing a `pobj` are seeded — the ADVERBIAL Hearst markers
-- ("especially"/"notably"/"e.g."/"for example") parse as `advmod` or a separate `for`-PP and would
-- never reach the chain, so seeding them would advertise coverage that does not exist. The growth
-- rail can add them once that dep shape is supported.
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency relations (xcomp / aux-mark "to" /
-- prep / pobj / nsubj), the POS guards, the `Tense=Past` and `Number=Plur` MORPHOLOGY tests, and the
-- _CATENATIVE/_MENTAL_STATE intent firewall. Only the LEXICAL vocabulary is DB data that grows. The
-- in-code `linguistics._IMPLICATIVE_VERB_LEMMAS` / `_EXEMPLIFICATION_MARKER_CUES` frozensets (and the
-- overlay's `_BOOTSTRAP_*` twins) are the DB-DOWN code-fallback seeds, updated in the SAME change so a
-- pre-migration / unwarmed-overlay turn behaves identically (fail-safe parity).
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit these automatically. This migration only (1) seeds the
-- rows into public and (2) fans them out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/191).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY)
-- ============================================================================
-- (a) implicative_verb — `cue` is matched against the spaCy verb LEMMA (lowercase). Membership is
-- corroborated downstream by the parse (Tense=Past matrix + an infinitival `to` xcomp) and gated by
-- the absolute _CATENATIVE/_MENTAL_STATE intent firewall. This list MIRRORS the in-code DB-DOWN seed
-- `linguistics._IMPLICATIVE_VERB_LEMMAS` / `linguistic_cue_overlay._BOOTSTRAP_IMPLICATIVE_VERBS`
-- EXACTLY (commit 31d90d7f) — keep them in lockstep. NOTE: `manage` and `happen` are seeded but are
-- currently BLOCKED at the call site because they also sit in `predicate_span._CATENATIVE`; that is a
-- deliberate, documented under-capture (over-asserting what the user did NOT do is the worse failure).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('have',     'implicative_verb', 'Modal-necessity matrix; PAST + perfective forces an actuality entailment (Bhatt 1999 / Hacquard 2006)', 'I had to take my mixer to a repair shop last month', 'seed_implicative', 0.88),
  ('get',      'implicative_verb', 'Positive implicative (Karttunen 1971): "got to X" entails X happened',                                   'I got to meet the CEO last Friday',                  'seed_implicative', 0.84),
  ('remember', 'implicative_verb', 'Positive implicative (Karttunen 1971): "remembered to X" entails X happened',                            'I remembered to renew my passport in January',       'seed_implicative', 0.84),
  ('bother',   'implicative_verb', 'Positive implicative (Karttunen 1971)',                                                                  'I bothered to file the complaint last week',         'seed_implicative', 0.78),
  ('dare',     'implicative_verb', 'Positive implicative (Karttunen 1971)',                                                                  'I dared to challenge the ruling last October',       'seed_implicative', 0.78),
  ('manage',   'implicative_verb', 'Karttunen paradigm implicative; currently blocked by the _CATENATIVE firewall (documented under-capture)', 'I managed to fix the dishwasher last week',         'seed_implicative', 0.80),
  ('happen',   'implicative_verb', 'Positive implicative (Karttunen 1971); currently blocked by the _CATENATIVE firewall',                    'I happened to meet her last Tuesday',                'seed_implicative', 0.70)
ON CONFLICT (cue, category) DO NOTHING;

-- (b) exemplification_marker — `cue` is the SPACE-JOINED lowercase marker SURFACE ("such as" is one
-- cue, not two). `description` carries the POLYSEMY MODE read by resolve_exemplification_marker_modes.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('such as',   'exemplification_marker', 'unambiguous',    'I have several pets, such as a dog',                'seed_hearst', 0.90),
  ('including', 'exemplification_marker', 'unambiguous',    'I visited many museums including the Louvre',       'seed_hearst', 0.88),
  ('like',      'exemplification_marker', 'comma_required', 'various workshops and lectures, like the workshop', 'seed_hearst', 0.72)
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
            WHERE category IN ('implicative_verb', 'exemplification_marker')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 196: implicative_verb + exemplification_marker cues seeded into %',
                     _schema;
    END LOOP;
END $$;
