-- Migration 322: linguistic_cues — seed the MEDICAL SHELF-NOUN cue class (BUG-FAMILY-9
-- residual 2, the medical class-query ground, 2026-11-13).
--
-- WHY
-- ---
-- The CLASS-shaped question "what are my medical conditions?" abstained honest-empty on a
-- seat carrying LIVE medical rows (metformin + penicillin both stored valid), while the
-- surface-shaped control ("what allergy should be flagged before prescriptions?") served
-- from the SAME seat — measured on the production door during the family-shape probe (the
-- sparse-store re-verify's filed residual). The recall walk's health-shelf admission
-- (RECALL_HEALTH_SHELF, BUG-FAMILY-7) matched its CLOSED in-code surface class
-- (allergy/allergies/allergic → has_allergy; medication(s)/medicine → has_medication), so
-- the query naming a class noun in that class admitted the shelf — but the admission was
-- single-rel (only the matched surface's rel was probed for live rows), and the
-- has_medical_condition shelf (migration 025, 314's sensitivity census) had NO query
-- surface at all: the noun "condition(s)" admitted nothing, and even a matched "medication"
-- question could not reach a sibling allergy row.
--
-- THE FIX (the read half is in src/api/main.py under the RECALL_HEALTH_SHELF_CLASS flag):
--   1. the class nouns become a SEEDED GROWABLE cue class (this migration, the 316/308
--      discipline): medical_shelf_noun, the shelf rel riding the row's `description`
--      (the kinship_noun 109 convention). A tenant grows its own surfaces ("rx",
--      "prescriptions") through the freq-gated rail without code edits.
--   2. the admission becomes CLASS-GROUPED: a question naming ANY seeded class noun
--      admits the anchor's live rows across the whole declared medical shelf class
--      (the 314 sensitivity census, medical subset — the closed seeded rel set mirrored
--      in main.py's _HEALTH_SHELF_REL_CLASS) — the CLASS query serves the CLASS rows,
--      never just one entity-matched row.
--
-- ROWS (medical_shelf_noun → shelf rel, the map convention: `description` carries the
-- canonical shelf rel — has_allergy / has_medication / has_medical_condition, all
-- migration-025 seeded, 314-stamped is_sensitive):
--   allergy / allergies / allergic            → has_allergy
--   medication / medications / medicine       → has_medication
--   condition / conditions                    → has_medical_condition
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category
-- and the provisioning seeder blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — new tenants inherit automatically. This migration only (1) seeds the
-- new category into public and (2) fans it out to EXISTING tenant schemas (the 316/320
-- pattern). Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/316).
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

-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY).
-- 'cue' is matched lowercase whole-word against the QUERY text; the row's `description`
-- carries the canonical shelf rel the class noun admits (the kinship_noun convention).
-- The seed is the ONE measured class — the migration-025 shelf names themselves; growth
-- is the class (a tenant-grown surface rides the same freq-gated rail as 316's members).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('allergy',     'medical_shelf_noun', 'has_allergy',
   'What allergy should be flagged before prescriptions?', 'seed_medical_shelf_nouns', 0.95),
  ('allergies',   'medical_shelf_noun', 'has_allergy',
   'What are my allergies?', 'seed_medical_shelf_nouns', 0.95),
  ('allergic',    'medical_shelf_noun', 'has_allergy',
   'Am I allergic to anything?', 'seed_medical_shelf_nouns', 0.92),
  ('medication',  'medical_shelf_noun', 'has_medication',
   'What medication did the doctor start?', 'seed_medical_shelf_nouns', 0.95),
  ('medications', 'medical_shelf_noun', 'has_medication',
   'What medications do I take?', 'seed_medical_shelf_nouns', 0.95),
  ('medicine',    'medical_shelf_noun', 'has_medication',
   'Did I mention any medicine I take?', 'seed_medical_shelf_nouns', 0.92),
  ('condition',   'medical_shelf_noun', 'has_medical_condition',
   'Do I have any medical condition on file?', 'seed_medical_shelf_nouns', 0.95),
  ('conditions',  'medical_shelf_noun', 'has_medical_condition',
   'What are my medical conditions?', 'seed_medical_shelf_nouns', 0.95)
ON CONFLICT (cue, category) DO NOTHING;

-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants.
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

        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category IN ('medical_shelf_noun')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 322: medical_shelf_noun seeded into %', _schema;
    END LOOP;
END $$;
