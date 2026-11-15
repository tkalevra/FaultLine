-- Migration 318: linguistic_cues — seed the PERSONAL-ATTRIBUTE SHELF class
-- (SEC-BUG-2's full shelf, the serve-side band gap; the C5 controls, 2026-11-12).
--
-- WHY
-- ---
-- SEC-BUG-2 (DEV/BUGS/family-smoke-20261111/SEC-BUG-2-control-recall-band-gap.md) is the
-- LEXICAL-TOPIC-GATE class: legitimate personal facts INGEST but their natural questions
-- abstain (or mis-serve a sibling attribute) because no walk admission can tie the
-- question's surface to the stored attribute's NAME. The allergy fix (BUG-FAMILY-7)
-- landed the HEALTH shelf (has_allergy/has_medication; the RECALL_HEALTH_SHELF walk
-- admission reads it); the broader C5 band stayed open, measured live on the deployed
-- pre-prod image 2026-11-12 (the C5 band probe, fresh seat through the REAL doors):
--   * "what is my bank account number?"  → MIS-SERVES the phone number ('number' is
--     phone_number's head token — ATTRSCOPE admits the sibling, the has_reference_id row
--     the question names sits dark). The row IS in the store (entity_attributes
--     has_reference_id = 123456789, census-verified).
--   * "what laptop do I work on?"        → honest abstention (the work_laptop attr's
--     surface "work laptop" never appears contiguously in the question; the phrase form
--     "what is my work laptop?" already serves).
--   * the medication class ("what medication am I on?" → "you Take Metformin") and the
--     contact class ("what is my phone number?" / email) ALREADY serve — measured on the
--     same seat; no shelf row needed for them.
--
-- THE CLASS (the 316 discipline): category ``personal_attr_shelf`` in
-- public.linguistic_cues, fanned to every existing tenant schema. The CUE is the
-- QUESTION surface (matched whole-phrase, or token-spread — all the surface's words
-- present in the question, order-free: "work laptop" matches "what laptop do I work
-- on?"); the DESCRIPTION carries the ATTRIBUTE NAME the surface maps to (the same
-- desc-carries-canonical shape the ``unit_scalar`` class uses for units). The code-side
-- floor in src/api/main.py carries the same members; the walk admission
-- (RECALL_PERSONAL_ATTR_SHELF, default ON) is metadata-coupled — a surface the anchored
-- entity holds NO live attribute for admits nothing, so the abstention stands honestly.
-- Growable per-tenant: a new (cue, category, description) row extends the shelf without
-- code edits (the overlay's floor ∪ tenant merge rule).
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category
-- and the provisioning seeder blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — new tenants inherit automatically. Idempotent: ON CONFLICT
-- (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/308/316).
CREATE TABLE IF NOT EXISTS public.linguistic_cues (
    id                SERIAL PRIMARY KEY,
    cue               VARCHAR(128) NOT NULL,
    category          VARCHAR(64) NOT NULL DEFAULT 'naming_verb',
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the measured members
-- ============================================================================
-- The cue surface is matched lowercase (whole-phrase word-boundary, or token-spread);
-- the description IS the canonical entity_attributes attribute name the surface admits.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('bank account',    'personal_attr_shelf', 'has_reference_id',
   'what is my bank account number?', 'seed_sec_bug_2', 0.92),
  ('account number',  'personal_attr_shelf', 'has_reference_id',
   'what is my account number?', 'seed_sec_bug_2', 0.9),
  ('reference number', 'personal_attr_shelf', 'has_reference_id',
   'what is my reference number?', 'seed_sec_bug_2', 0.9),
  ('work laptop',     'personal_attr_shelf', 'work_laptop',
   'what laptop do I work on?', 'seed_sec_bug_2', 0.92),
  ('work computer',   'personal_attr_shelf', 'work_laptop',
   'what is my work computer?', 'seed_sec_bug_2', 0.9),
  ('work machine',    'personal_attr_shelf', 'work_laptop',
   'how big is my work machine''s screen?', 'seed_sec_bug_2', 0.9)
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
                category          VARCHAR(64) NOT NULL DEFAULT 'naming_verb',
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
            WHERE category IN ('personal_attr_shelf')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 318: personal_attr_shelf seeded into %', _schema;
    END LOOP;
END $$;
