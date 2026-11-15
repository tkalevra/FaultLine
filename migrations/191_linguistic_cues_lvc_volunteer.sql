-- Migration 191: linguistic_cues — add "volunteer" to the LVC SUPPORT-VERB cue class.
-- Date: 2026-07-22
--
-- WHY
-- ---
-- A participation event is very often stated with the verb "volunteer" — "I volunteered at the
-- 'Food for Thought' event", "I volunteered at a beach cleanup on July 3rd". "volunteer" is a
-- genuine PARTICIPATION/ATTENDANCE support verb of the SAME closed grammatical (light/participation-
-- verb) class as the seeded "attend"/"participate": it carries no content of its own — the eventive
-- noun it governs (event/cleanup/drive) is the semantic head — so it forms a light-verb construction
-- exactly like "attended a workshop" / "participated in a webinar". It was simply missing from the
-- seed, so ``linguistics.analyze_events`` returned [] for every "I volunteered at <event>" clause and
-- the dated participation never reified (LME q a3838d2b — event-participation multi-mention
-- undercapture; only "participated in"/"attended" phrasings landed).
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency relations (nsubj / dobj /
-- governed-prep pobj / conj), the POS guards, and the 1st-person-subject morphology test that
-- CORROBORATE the construction downstream (a "volunteer" that does NOT govern an eventive head noun
-- never mints an occurrence). Only the verb-LEMMA vocabulary is DB data that grows. The in-code
-- ``linguistics._LVC_SUPPORT_VERB_LEMMAS`` frozenset (the DB-DOWN code-fallback seed) is updated in
-- the SAME change so a pre-migration / unwarmed-overlay turn also detects it (fail-safe parity).
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit this cue automatically. This migration only (1) seeds the
-- row into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/189).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the volunteer LVC cue
-- ============================================================================
-- `cue` is matched against the spaCy verb lemma (lowercase). Membership is corroborated downstream by
-- the parse (a support verb governing an eventive dobj / governed-prep pobj under a 1st-person subject).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('volunteer', 'lvc_support_verb', 'Participation/attendance support verb (governed-prep eventive object)', 'I volunteered at a charity event', 'seed_lvc_support', 0.86)
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
            WHERE category = 'lvc_support_verb' AND cue = 'volunteer'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 191: volunteer lvc_support_verb cue seeded into %', _schema;
    END LOOP;
END $$;
