-- Migration 105: linguistic_cues — DB-held, per-tenant, GROWABLE linguistic-verb cue engine
-- Date: 2026-06-20
--
-- WHY
-- ---
-- The deterministic LINGUISTIC LAYER (src/extraction/linguistics.py) recovers a NAMING/dubbing
-- construction ("a dog named Rex", "the workshop called X") by matching the modifying verb's
-- LEMMA against a FROZEN in-code two-word set (`_NAMING_VERB_LEMMAS = frozenset({"name","call"})`).
-- A frozen list assumes a fixed naming vocabulary and silently DROPS every other English naming
-- verb ("titled"/"dubbed"/"christened"/"entitled"/"designated"/"termed"/"labelled"/"nicknamed").
-- That is exactly the "frozen list pretending to be a rule" brittleness the rest of the engine
-- forbids — rel_types / entity_taxonomies / extraction_patterns / temporal_patterns are all DB-held
-- + overlay-resolved + freq-gated growable. This table moves the VERB VOCABULARY into per-tenant DB
-- rows, MIRRORING `temporal_patterns` (migration 103) for lifecycle/confidence and the rel_type /
-- taxonomy / temporal overlay contract for per-tenant resolution + growth.
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data):
--   * the dependency RELATIONS (`acl`/`relcl`/`compound`/`appos`/`oprd`/`attr`/`dobj` …) — grammar,
--   * the universal POS function-word tag set (`_FUNCTION_POS`) — a language primitive,
--   * the spaCy dependency parse (the DETECTOR).
-- Only the naming-VERB lemma RECOGNITION (`_NAMING_VERB_LEMMAS` membership) becomes DB data that
-- grows. The dependency-relation branches in `analyze_naming` / `_event_title` are NOT data.
--
-- CATEGORY SEMANTICS (category)
-- -----------------------------
-- This table is GENERAL by `category` so the SAME rail can later hold other linguistic-verb cue
-- classes (e.g. `lvc_support_verb` — the light-verb class in `analyze_event`) WITHOUT a new table:
--   * 'naming_verb'      — the predicative naming/dubbing verb lemma class ("name"/"call"/"title"/
--                          "dub"/…). THIS is the only class this migration seeds + grows.
--   * (reserved)         — other verb-cue classes are NOT seeded here; they migrate onto this rail
--                          only when their seam is converted off its own in-code frozenset.
--
-- THE SEED IS EVIDENCED ENGLISH NAMING/DUBBING VERBS (NOT an arbitrary port)
-- -------------------------------------------------------------------------
-- The seed is the small, bounded English predicative-naming verb class — the verbs that form the
-- "X <verb> Y" naming/dubbing construction (lexical-aspect class, like the copula "be" or the LVC
-- light verbs). Stored as LEMMAS (the overlay matches on spaCy token lemma, casing-robust):
--   name, call, title, dub, entitle, christen, designate, term, label, nickname
-- Kept MINIMAL (enough zero-shot coverage; growth handles the long tail). public = seed-FROM only.
--
-- PER-TENANT: runtime search_path EXCLUDES public, so public rows are template/seed only. New
-- tenants inherit via the provisioning seeder (INSERT ... SELECT FROM public.linguistic_cues,
-- schema_manager.py) + the user_schema.sql DDL. EXISTING tenants are backfilled by the DO loop
-- below (mirrors 103's fan-out). Idempotent: ON CONFLICT DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL.

-- ============================================================================
-- Part 0: shared DDL (public + the per-tenant fan-out reuse the SAME column list)
-- ============================================================================
-- Columns MIRROR temporal_patterns (103) for lifecycle/confidence, with `cue` replacing
-- `pattern_regex` (a naming cue is a VERB LEMMA, matched by equality on the parse lemma — NOT a
-- regex) and `category` replacing `anchor_type` as the class discriminator.
--   cue               — the VERB LEMMA matched against the spaCy token lemma ("name", "title").
--   category          — the cue class ('naming_verb' for all seeded rows; a grown naming verb keeps it).
--   source            — traceable origin ('seed_naming_dubbing' | 'grown').
--   confidence/lifecycle cols mirror temporal_patterns for the re-embedder growth/decay sweep.

CREATE TABLE IF NOT EXISTS public.linguistic_cues (
    id                SERIAL PRIMARY KEY,
    cue               VARCHAR(128) NOT NULL,
    category          VARCHAR(64)  NOT NULL DEFAULT 'naming_verb',

    -- Confidence metrics (weak supervision — mirrors temporal_patterns / extraction_patterns)
    frequency         INT   DEFAULT 0,
    confirmed_count   INT   DEFAULT 0,
    rejected_count    INT   DEFAULT 0,
    correction_count  INT   DEFAULT 0,
    global_confidence FLOAT DEFAULT 0.5,

    -- Metadata
    description       TEXT,
    example_text      TEXT,
    source            VARCHAR(64),   -- 'seed_naming_dubbing' | 'grown'

    -- Lifecycle (mirrors temporal_patterns)
    is_active         BOOLEAN DEFAULT true,
    archived_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW(),
    last_matched_at   TIMESTAMP,

    UNIQUE (cue, category)
);

CREATE INDEX IF NOT EXISTS idx_linguistic_cues_active
    ON public.linguistic_cues(is_active, category);
CREATE INDEX IF NOT EXISTS idx_linguistic_cues_category
    ON public.linguistic_cues(category);

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the naming/dubbing verb class
-- ============================================================================
-- `cue` is a lowercase VERB LEMMA; the overlay reader matches it against the spaCy token lemma
-- (case-insensitive). Only category='naming_verb' is seeded now.

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('name',     'naming_verb', 'Predicative naming verb: "<noun> named X"',     'a dog named Rex',                  'seed_naming_dubbing', 0.95),
  ('call',     'naming_verb', 'Predicative naming verb: "<noun> called X"',    'a server called Atlas',               'seed_naming_dubbing', 0.95),
  ('title',    'naming_verb', 'Dubbing verb: "<noun> titled X"',               'a workshop titled Effective Time Mgmt', 'seed_naming_dubbing', 0.90),
  ('dub',      'naming_verb', 'Dubbing verb: "<noun> dubbed X"',               'a release dubbed Jaguar',              'seed_naming_dubbing', 0.85),
  ('entitle',  'naming_verb', 'Dubbing verb: "<noun> entitled X"',             'a talk entitled Memory Systems',       'seed_naming_dubbing', 0.85),
  ('christen', 'naming_verb', 'Naming verb: "<noun> christened X"',            'a ship christened Endeavour',          'seed_naming_dubbing', 0.80),
  ('designate','naming_verb', 'Naming verb: "<noun> designated X"',            'a node designated Primary',            'seed_naming_dubbing', 0.78),
  ('term',     'naming_verb', 'Naming verb: "<noun> termed X"',                'a phase termed Discovery',             'seed_naming_dubbing', 0.72),
  ('label',    'naming_verb', 'Naming verb: "<noun> labelled X"',              'a branch labelled Stable',            'seed_naming_dubbing', 0.75),
  ('nickname', 'naming_verb', 'Naming verb: "<noun> nicknamed X"',             'a dog nicknamed Bug',                  'seed_naming_dubbing', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- Create the table in each tenant schema (search_path has NO public at runtime) and seed it
-- from public. Mirrors 103's fan-out. Idempotent: ON CONFLICT DO NOTHING.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- ---- table DDL (tenant-local) ----
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
                example_text     TEXT,
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

        -- ---- seed from public (template) ----
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 105: linguistic_cues created + seeded into %', _schema;
    END LOOP;
END $$;
