-- Migration 279: linguistic_cues — seed the `measure_verb` cue class (VERB-MEASURE scalar lane)
-- Date: 2026-09-17 (issue #9 — verb-measure frame silent drop)
--
-- WHY
-- ---
-- The VERB-MEASURE frame ("An adult blue-ringed octopus measures about 5 centimeters across.")
-- fell through every scalar lane: spaCy MIS-TAGS "measures" NOUN/NNS and ROOTs the clause, so the
-- sentence has NO VERB and every verb-gated gate declines (measured on pre-prod: spine 0 edges,
-- DETECT-ONLY rewrite 0, no_ingest — the numeral AND unit annihilated, the CLAUDE.md "59% of
-- misses lost a NUMBER" class). The lane's mis-tag arm (linguistics VERB_MEASURE_SCALAR V2)
-- recovers the measurement READING of that noun when its LEMMA resolves in this DB-grown,
-- per-tenant, growable `measure_verb` class — the SAME (cue, category) rail as kinship_noun /
-- unit_scalar / naming_verb (migration 109 pattern). The frame STRUCTURE (compound subject +
-- nummod'd unit NP + the shared NER QUANTITY discriminator) still gates; the cue class only
-- replaces the POS tag the mis-tag destroyed. A properly tagged verb never consults this class.
--
-- The floor is deliberately SMALL (measure/weigh/span): only verbs whose measurement sense is a
-- LEXICAL fact (Quirk et al., CGEL ch. 9 — they take a measure-phrase complement as their core
-- frame). A polysemous verb (run/cost/last) joins by TENANT GROWTH (freq-gated cue candidates),
-- never by widening the seed.
--
-- NO DDL CHANGE: 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories
-- (minus the two carved) into every NEW tenant — new tenants inherit this class automatically.
-- This migration only (1) seeds the new category into public and (2) fans it out to EXISTING
-- tenant schemas. Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the new class
-- ============================================================================

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('measure', 'measure_verb', 'measurement verb: takes a measure-phrase complement (CGEL ch.9)',
   'an adult blue-ringed octopus measures about 5 centimeters across', 'seed_measure_verb', 0.95),
  ('weigh',   'measure_verb', 'measurement verb: mass measure-phrase complement (CGEL ch.9)',
   'the hatchling weighs about 30 grams', 'seed_measure_verb', 0.95),
  ('span',    'measure_verb', 'measurement verb: extent measure-phrase complement (CGEL ch.9)',
   'the bridge spans nearly 2 kilometers', 'seed_measure_verb', 0.92)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (105 / user_schema.sql). Create it if missing (defensive,
-- same DDL), then seed the NEW category from public. Mirrors 109's fan-out. Idempotent.

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

        -- ---- seed the NEW category from public (template) ----
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category = 'measure_verb'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 279: measure_verb cues seeded into %', _schema;
    END LOOP;
END $$;
