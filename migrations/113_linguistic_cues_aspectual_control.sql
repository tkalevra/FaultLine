-- Migration 113: linguistic_cues — seed the ASPECTUAL / phase SUBJECT-CONTROL verb cue class
-- Date: 2026-06-22
--
-- WHY
-- ---
-- "I started working with Rachel on 2/15." minted NO edge. spaCy splits the subject and the object
-- across TWO verb tokens: the ASPECTUAL/phase matrix ("started", ROOT) carries the SUBJECT ("I") and
-- raises it, leaving the REALIZED activity verb ("working") as an `xcomp` that carries the OBJECT
-- ("Rachel"). Every flat SVO lane (linguistics.analyze_svo_relations / the deriver's _chain_svo)
-- requires the subject AND object on the SAME verb, so the matrix had a subject but no object and the
-- xcomp had the object but no subject — the engine derived `work_with` on the xcomp then DISCARDED it
-- for lack of a co-located subject. (Contrast "I work with Rachel" → (user, work_with, rachel) ✅.)
--
-- THE FIX (engine-generic, NOT a special case): `linguistics._aspectual_activity_xcomp` licenses
-- DESCENDING into a progressive `-ing` activity `xcomp` of an ASPECTUAL/phase matrix and re-runs the
-- SAME SVO recovery there, using the MATRIX subject — minting (user, work_with, rachel) via the exact
-- machinery that already nails "I work with Rachel". Generalizes to begin/keep/continue/resume/finish/
-- stop ("I began managing the Anderson account", "I kept emailing Tom" …).
--
-- THE GATE (deterministic grammar; NO new hardcoded verb list at the call site beyond this cue class):
--   (1) matrix lemma ∈ this ASPECTUAL/phase class (DB-held, grown on the SAME rail), AND
--   (2) the xcomp is a PROGRESSIVE `-ing` VERB (tag VBG / morph Aspect=Prog) — a REALIZED activity,
--       NOT an infinitival `to`-complement (UNREALIZED INTENT: "started to think", "want to buy"), AND
--   (3) the matrix is NOT a CATENATIVE / MENTAL-STATE verb (predicate_span._CATENATIVE / _MENTAL_STATE
--       — "considered hiring", "like working" take an -ing complement too but predicate desire/opinion,
--       not a stated occurrence). "User is truth": an intention is not a thing the user did.
--
-- DELIBERATELY DISTINCT from the `inchoative_verb` category (migration 112): the inchoative rail feeds
-- `analyze_inchoative` (a NOUN-object "started <item>" ingressive occurrence), where adding the
-- continuative/terminative phase verbs (keep/continue/resume/finish/stop) would mis-mint "I kept the
-- receipt" → a (user, participated_in, receipt) occurrence. SAME table / SAME overlay machinery, a
-- SEPARATE aspectual category — so the two seams grow independently and never cross-contaminate.
--
-- DB-HELD + per-tenant + GROWABLE — NOT a frozen in-code list. The in-code
-- `linguistics._ASPECTUAL_CONTROL_VERB_LEMMAS` / `linguistic_cue_overlay._BOOTSTRAP_ASPECTUAL_CONTROL_VERBS`
-- frozensets REMAIN only as the DB-DOWN code-fallback seed (fail-safe; never lose the descent on a
-- pre-migration / unwarmed-overlay turn).
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit this class automatically. This migration only (1) seeds the
-- new category into public and (2) fans it out to EXISTING tenant schemas.
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the aspectual-control class
-- ============================================================================
-- `cue` is matched against the spaCy token lemma (verbs), lowercase. Membership is corroborated
-- downstream by the parse (a progressive -ing activity xcomp + the catenative/mental-state firewall).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('start',    'aspectual_control_verb', 'Ingressive phase verb raising the subject over a progressive -ing activity', 'I started working with Rachel on 2/15', 'seed_aspectual_control', 0.90),
  ('begin',    'aspectual_control_verb', 'Ingressive phase verb raising the subject over a progressive -ing activity', 'I began managing the Anderson account',  'seed_aspectual_control', 0.90),
  ('continue', 'aspectual_control_verb', 'Continuative phase verb raising the subject over a progressive -ing activity', 'I continued reviewing the report',     'seed_aspectual_control', 0.88),
  ('keep',     'aspectual_control_verb', 'Continuative phase verb raising the subject over a progressive -ing activity', 'I kept emailing Tom',                   'seed_aspectual_control', 0.85),
  ('resume',   'aspectual_control_verb', 'Continuative phase verb raising the subject over a progressive -ing activity', 'I resumed training the model',          'seed_aspectual_control', 0.85),
  ('commence', 'aspectual_control_verb', 'Ingressive phase verb (formal) raising the subject over a progressive -ing activity', 'I commenced reviewing the audit', 'seed_aspectual_control', 0.80),
  ('finish',   'aspectual_control_verb', 'Terminative phase verb raising the subject over a progressive -ing activity', 'I finished writing the report',         'seed_aspectual_control', 0.82),
  ('stop',     'aspectual_control_verb', 'Terminative phase verb raising the subject over a progressive -ing activity', 'I stopped using Slack',                 'seed_aspectual_control', 0.80)
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
            WHERE category = 'aspectual_control_verb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 113: aspectual_control_verb cues seeded into %', _schema;
    END LOOP;
END $$;
