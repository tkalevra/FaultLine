-- Migration 116: linguistic_cues — seed the PROBLEM-NOUN (bland eventive head) cue class.
-- Date: 2026-06-26
--
-- WHY
-- ---
-- "I had an issue with my car's GPS system on 3/22" reifies a SEMANTICALLY-EMPTY eventive head
-- ("issue"/"problem"/"trouble") under the LVC seam, and the SPECIFIC affected entity ("GPS system")
-- sits in the with-PP. Today the deriver only proposes `(user, participated_in, gps system)` (an
-- argument-promoted bland head) — which Stage-0's tail-type veto correctly CRATERS to Class C (a GPS
-- is an Object, not an Event), so the device never becomes a durable, WALKABLE state filed under the
-- car. The user can ask "what issues have I had with my car?" and get NOTHING.
--
-- This cue class lets the deriver emit a COMPETING candidate `(<affected>, has_state, <problem-state>)`
-- — the affected entity's PROBLEM as a typed reusable state node (the structural twin of `feels`).
-- With both candidates present, the committed Stage-2 arbitration (`_arbitrate_spine_candidates`,
-- main.py) picks the STRONG `has_state` over the CRATER `participated_in`, so the GPS is cast as a
-- device STATE, keeps its `part_of car` / `owns` grounding from the other chains, and becomes walkable
-- from the car.
--
-- ⚠️ FLAGGED BOUNDED LEXICAL CLASS (honestly documented, EXACTLY like the naming / lvc_support /
-- inchoative / aspectual_control / acquisition classes already on this rail). The PROBLEM reading
-- cannot be made purely structural: "had an issue with X" and "had a meeting with X" share the SAME
-- dep shape (light-verb → dobj NOUN → with-PP). Only the head noun's lexical semantics distinguishes a
-- problem/fault state from a neutral occurrence, so a small bounded NOUN class is unavoidable. It is
-- firewalled downstream by the parse the SAME way the others are: the state reading fires ONLY when
-- (a) the head is in this `problem_noun` class AND (b) a with-PP supplies an affected entity — a
-- non-problem "have + with-PP" ("I had a meeting with Sarah", "I had lunch with Tom", "I had a call
-- with the team") is UNTOUCHED (head ∉ problem_noun). DB-HELD + per-tenant + GROWABLE (category=
-- 'problem_noun') so a tenant grows its own problem heads (freq-gated) without code edits. The in-code
-- `linguistic_cue_overlay._BOOTSTRAP_PROBLEM_NOUNS` frozenset REMAINS only as the DB-DOWN code-fallback
-- seed (fail-safe; never lose detection on a pre-migration / unwarmed-overlay turn).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (dobj/obj for the bland
-- head, prep(with)/pobj for the affected entity), the POS guards, the 1st-person-subject morphology
-- test. Only the PROBLEM-NOUN lemma recognition is DB data that grows. NO noun list in .py logic — the
-- discriminator lives in this DB cue class.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit this class automatically. This migration only (1) seeds
-- the new category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/112/115).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the problem-noun class
-- ============================================================================
-- `cue` is matched against the spaCy head-noun lemma (lowercase). Membership is corroborated
-- downstream by the parse (1st-person LVC subject + a with-PP affected entity off the bland head).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('issue',      'problem_noun', 'Bland problem/fault eventive head; with-PP supplies the affected entity', 'I had an issue with my car''s GPS', 'seed_problem_noun', 0.90),
  ('problem',    'problem_noun', 'Bland problem/fault eventive head',                                        'I had a problem with my router',   'seed_problem_noun', 0.90),
  ('trouble',    'problem_noun', 'Bland problem/fault eventive head',                                        'I had trouble with my laptop',     'seed_problem_noun', 0.88),
  ('fault',      'problem_noun', 'Bland problem/fault eventive head',                                        'I had a fault with the wiring',     'seed_problem_noun', 0.85),
  ('difficulty', 'problem_noun', 'Bland problem/fault eventive head',                                        'I had difficulty with the printer', 'seed_problem_noun', 0.82),
  ('glitch',     'problem_noun', 'Bland problem/fault eventive head (informal)',                             'I had a glitch with the app',       'seed_problem_noun', 0.80),
  ('bug',        'problem_noun', 'Bland problem/fault eventive head (software/informal)',                    'I had a bug with the build',        'seed_problem_noun', 0.72),
  ('error',      'problem_noun', 'Bland problem/fault eventive head',                                        'I had an error with the upload',    'seed_problem_noun', 0.78),
  ('concern',    'problem_noun', 'Bland problem/fault eventive head',                                        'I had a concern with the contract', 'seed_problem_noun', 0.68)
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
            WHERE category = 'problem_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 116: problem_noun cues seeded into %', _schema;
    END LOOP;
END $$;
