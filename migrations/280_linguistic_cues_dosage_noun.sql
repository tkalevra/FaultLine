-- Migration 280: linguistic_cues — seed the `dosage_noun` cue class (issue #18)
-- Date: 2026-09-19 (issue #18 — dosage attribute-family incoherence across the three seams)
--
-- WHY
-- ---
-- The dosage attribute family was named DIFFERENTLY at every seam (deploy #9, measured):
--   (a) INGEST  — the #14 take-frame stores the generic `quantity` scalar on the substance
--       ("I take lisinopril 10 milligrams daily" → (lisinopril, quantity, "10 milligrams"));
--   (b) INGEST  — the copula/attr-scalar frame WELDS the family noun into the attribute name on
--       the speaker ("My lisinopril dose is 10 milligrams" → (user, lisinopril_dose, …); the live
--       LLM-atomized path minted the same weld as a 'Lisinopril Dosage' phrase ENTITY);
--   (c) CORRECTION — the correction-extraction LLM names a fresh rel ('has_medication'), the #14
--       `_dosage_correction_frame_floor` cannot fire (it is gated to a NO-rel gap only), every
--       addressing rung declines on the mint → "Old relational fact not found: has_medication";
--   (d) QUERY — "What is my X dose/level?" binds only through measure_verb (verbs-only seed) and
--       the `quantity` constant, so a `*_level`/`*_dose` row is reachable only via the fetch-all.
--
-- ONE FAMILY RESOLUTION: the dose/dosage/quantity/level/amount family resolves through THIS
-- DB-grown, per-tenant, growable cue class (the SAME (cue, category) rail as kinship_noun /
-- measure_verb / unit_scalar — migration 109/279 pattern). The rows ALSO carry the family's
-- canonical attribute in `description` (the keyed-map contract, like kinship_noun's noun→rel map):
-- every seeded member canonicalizes to `quantity` — the attribute the #14 take-frame already
-- writes — so all three seams resolve to ONE stored name. The noun SLOT is open: a tenant grows
-- its own family members (freq-gated candidates) and points each row's description at its
-- canonical; nothing in code enumerates dosage vocabulary (NO word zoo — the code reads this class
-- through the overlay resolvers only).
--
-- NO DDL CHANGE: 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder blanket-copies ALL public.linguistic_cues categories into every NEW tenant —
-- new tenants inherit this class automatically. This migration only (1) seeds the new category
-- into public and (2) fans it out to EXISTING tenant schemas. Idempotent:
-- ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the new class
-- ============================================================================

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('dose',    'dosage_noun', 'quantity',
   'my lisinopril dose is 10 milligrams', 'seed_dosage_noun', 0.95),
  ('dosage',  'dosage_noun', 'quantity',
   'what is my lisinopril dosage?', 'seed_dosage_noun', 0.95),
  ('quantity','dosage_noun', 'quantity',
   'I take lisinopril 10 milligrams daily', 'seed_dosage_noun', 0.95),
  ('level',   'dosage_noun', 'quantity',
   'my thyroid level is 2.1', 'seed_dosage_noun', 0.95),
  ('amount',  'dosage_noun', 'quantity',
   'the amount I take is 20 milligrams', 'seed_dosage_noun', 0.95)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (105 / user_schema.sql). Create it if missing (defensive,
-- same DDL), then seed the NEW category from public. Mirrors 279's fan-out. Idempotent.

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
            WHERE category = 'dosage_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 280: dosage_noun cues seeded into %', _schema;
    END LOOP;
END $$;
