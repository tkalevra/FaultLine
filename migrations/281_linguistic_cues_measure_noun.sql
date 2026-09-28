-- Migration 281: linguistic_cues — seed the `measure_noun` cue class (issue #19 W1)
-- Date: 2026-09-19 (issue #19 wound 1 — possessive-quantity L4 beyond the dosage floor)
--
-- WHY
-- ---
-- The #18 dosage-family rail (migration 280) resolved the dose/dosage/quantity/level/amount
-- family at all three seams, but deploy-#10 measured the SAME copula weld for every
-- measurement family BEYOND that floor (issue #19 W1, rows pulled live):
--   (a) "My bench press personal record is 140 kilograms." → (user, owns, bench) island +
--       (record, weight, '140') — the amount detached on a bare 'record' entity, unit dropped;
--   (b) "My bouillabaisse recipe uses 800 grams of fish." → the flat user weld
--       (user, bouillabaisse_recipe, …) + the (recipe, use, fish) / (fish, quantity, …)
--       fragments — no L4 link from the user to the recipe's quantity;
--   (c) 'personal record' / 'recipe' are NOT members of dosage_noun — the owner ruling:
--       they must become reachable through GROWTH (cue rails), never code enumeration.
--
-- THIS rail is that growth path: the measurement-family noun class beyond dosage, on the SAME
-- (cue, category) keyed-map contract (description = the family CANONICAL attribute). The
-- generalized ingest rebind (linguistics SPINE_POSSESSED_QUANTITY_L4) resolves
-- dosage_noun ∪ measure_noun, so a first-person possessed "my <X> <measure-noun> is <NUM unit>"
-- rebinds possessor→the substance X and attribute→the family canonical — the measurement
-- hangs OFF the measured thing through L4, never minting a phrase-entity island.
--
-- THE FLOOR IS DELIBERATELY TWO MEMBERS, both measurement nouns whose measured sense is a
-- LEXICAL FACT (CGEL ch.5 §8 measure nouns: they denote a quantity on a scale):
--   • record     — a registered best measurement ("personal record", "course record");
--   • vocabulary — a counted lexicon size ("a vocabulary of 2000 words").
-- Each canonicalizes to ITSELF (a singleton family keeps its own name; the keyed description
-- exists so TENANT GROWTH can unify synonyms — 'pr' → record — exactly the way dosage unifies
-- dose → quantity). 'personal' is an amod, not the head; the rebind matches HEAD lemmas only,
-- so no 'personal record' row is needed. A polysemous noun (pressure/budget/limit) joins by
-- TENANT GROWTH (freq-gated cue candidates), never by widening this floor.
--
-- NO DDL CHANGE: 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder blanket-copies ALL public.linguistic_cues categories into every NEW
-- tenant — new tenants inherit this class automatically. This migration only (1) seeds the new
-- category into public and (2) fans it out to EXISTING tenant schemas. Idempotent:
-- ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the new class
-- ============================================================================

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('record',     'measure_noun', 'record',
   'my bench press personal record is 140 kilograms', 'seed_measure_noun', 0.95),
  ('vocabulary', 'measure_noun', 'vocabulary',
   'my español vocabulary is about 2000 words', 'seed_measure_noun', 0.95)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (105 / user_schema.sql). Create it if missing (defensive,
-- same DDL), then seed the NEW category from public. Mirrors 280's fan-out. Idempotent.

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
            WHERE category = 'measure_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 281: measure_noun cues seeded into %', _schema;
    END LOOP;
END $$;
