-- Migration 103: temporal_patterns — DB-held, per-tenant, GROWABLE relative-cue engine
-- Date: 2026-06-19
--
-- WHY
-- ---
-- The temporal date-MATCHING layer (src/extraction/linguistics.py) split a candidate date
-- SPAN into {explicit_year | absolute_no_year | relative} so dateparser is anchored correctly
-- (prefer-past for relatives; closest-to-reference YEAR anchoring for an absolute month-day).
-- That split is correct, but the RELATIVE-cue recognition was a FROZEN in-code word-list
-- (`_RELATIVE_DATE_CUES`). A frozen list assumes a fixed temporal vocabulary and silently
-- mis-anchors anything outside it — exactly the brittleness the rest of the engine forbids
-- (cf. rel_types / entity_taxonomies / extraction_patterns are all DB-held + overlay-resolved
-- + freq-gated growable). This table moves the RELATIVE cue inventory into per-tenant DB rows,
-- mirroring `extraction_patterns` (migration 058) for lifecycle/confidence and the rel_type /
-- taxonomy overlay contract for per-tenant resolution + growth.
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data):
--   * the 12 month names + numeric M/D span detection,
--   * `_EXPLICIT_YEAR_RE` (a 4-digit year token),
--   * `_anchor_absolute_year` (closest-to-reference YEAR math),
--   * spaCy DATE NER + dateparser (the DETECTORS / normalizer).
-- Only the RELATIVE-cue recognition (`_span_is_relative`) becomes DB data that grows.
--
-- ANCHOR SEMANTICS (anchor_type)
-- ------------------------------
--   * 'relative'         — the span resolves AGAINST the session reference and prefer-past is
--                          correct ("three weeks ago", "last Tuesday", "yesterday"). THIS is the
--                          only class this table grows. A span matching an ACTIVE relative-cue row
--                          (and carrying NO explicit 4-digit year) classifies relative.
--   * 'absolute_no_year' / 'explicit_year' — reserved discriminators for completeness; the closed
--                          FORMAL absolute checks stay in code (month-name / numeric / 4-digit year)
--                          so these classes are NOT seeded here. The table is the RELATIVE inventory.
--
-- THE SEED IS ONLINE-EVIDENCED (NOT a port of the old code list)
-- --------------------------------------------------------------
-- Every relative-cue row is sourced from the EVIDENCED rule-based prior art, NOT the retired
-- `_RELATIVE_DATE_CUES`. Each row's `source` is traceable to its origin:
--   * 'seed_dateparser_en' — dateparser's OWN English relative terminology (the in-stack rule
--       engine FaultLine already uses). Verified against its locale data
--       (dateparser_data/.../date_translation_data/en.yaml):
--         past markers   ago: [ago, before]
--         future markers in:  [in, from now, after]      (+ "later" via simplifications "... later → in ...")
--         relative-type phrases: "day before yesterday", "day after tomorrow", "till date"
--         deictic + units (CLDR base): today, yesterday, tomorrow, tonight, now,
--                                      decade, year, month, week, day, hour, minute, second
--   * 'seed_heideltime'    — HeidelTime English repattern resources (TIMEX3 prior art).
--       Verified against resources_repattern_reThisNextLast.txt:
--         last, past, next, latest, current, this, previous
--       and the widely TIMEX3-attested deictic "recent" / "upcoming"/"coming" relative indicators.
-- Kept MINIMAL (enough zero-shot coverage; growth handles the tail). public = seed-FROM only.
--
-- PER-TENANT: runtime search_path EXCLUDES public, so public rows are template/seed only. New
-- tenants inherit via the provisioning seeder (INSERT ... SELECT FROM public.temporal_patterns,
-- schema_manager.py) + the user_schema.sql DDL. EXISTING tenants are backfilled by the DO loop
-- below (mirrors 091/058 fan-out). Idempotent: ON CONFLICT DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL.

-- ============================================================================
-- Part 0: shared DDL (public + the per-tenant fan-out reuse the SAME column list)
-- ============================================================================
-- Columns MIRROR extraction_patterns (058) for lifecycle/confidence + add anchor_type.
--   pattern_regex   — a word-boundary regex matching the relative CUE in a span (surface only).
--   anchor_type     — 'relative' (the grown class) | 'absolute_no_year' | 'explicit_year'.
--   category        — 'relative_cue' for all seeded rows (a grown row keeps 'relative_cue').
--   source          — traceable origin ('seed_dateparser_en' | 'seed_heideltime' | 'grown').
--   confidence/lifecycle cols mirror extraction_patterns for the re-embedder growth/decay sweep.

CREATE TABLE IF NOT EXISTS public.temporal_patterns (
    id                SERIAL PRIMARY KEY,
    pattern_regex     VARCHAR(1024) NOT NULL,
    anchor_type       VARCHAR(32)  NOT NULL DEFAULT 'relative',

    -- Confidence metrics (weak supervision — mirrors extraction_patterns)
    frequency         INT   DEFAULT 0,
    confirmed_count   INT   DEFAULT 0,
    rejected_count    INT   DEFAULT 0,
    correction_count  INT   DEFAULT 0,
    global_confidence FLOAT DEFAULT 0.5,

    -- Metadata
    description       TEXT,
    example_text      TEXT,
    category          VARCHAR(64),
    source            VARCHAR(64),   -- 'seed_dateparser_en' | 'seed_heideltime' | 'grown'

    -- Lifecycle (mirrors extraction_patterns)
    is_active         BOOLEAN DEFAULT true,
    archived_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW(),
    last_matched_at   TIMESTAMP,

    CONSTRAINT chk_temporal_anchor_type
        CHECK (anchor_type IN ('relative', 'absolute_no_year', 'explicit_year')),
    UNIQUE (pattern_regex, anchor_type)
);

CREATE INDEX IF NOT EXISTS idx_temporal_patterns_active
    ON public.temporal_patterns(is_active, anchor_type);
CREATE INDEX IF NOT EXISTS idx_temporal_patterns_category
    ON public.temporal_patterns(category);

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the EVIDENCED inventory
-- ============================================================================
-- pattern_regex is a word-boundary regex over the lowercased span surface (the overlay reader
-- compiles it case-insensitively). Multi-word cues use \s+ so spacing variants match.

INSERT INTO public.temporal_patterns
    (pattern_regex, anchor_type, description, example_text, category, source, global_confidence)
VALUES
  -- ── dateparser English: PAST markers (en.yaml  ago: [ago, before]) ──────────────
  ('\bago\b',                 'relative', 'Past marker: "<N units> ago"',            'three weeks ago',       'relative_cue', 'seed_dateparser_en', 0.95),
  ('\bbefore\b',              'relative', 'Past marker: "<N units> before"',          'two days before',       'relative_cue', 'seed_dateparser_en', 0.80),

  -- ── dateparser English: FUTURE markers (en.yaml  in: [in, from now, after]; "later" via simplifications) ──
  ('\bfrom\s+now\b',          'relative', 'Future marker: "<N units> from now"',      'two weeks from now',    'relative_cue', 'seed_dateparser_en', 0.92),
  ('\blater\b',               'relative', 'Future marker: "<N units> later" (simplifications → in)', 'three days later', 'relative_cue', 'seed_dateparser_en', 0.85),

  -- ── dateparser English: relative-type PHRASES (en.yaml relative-type:) ──────────
  ('\bday\s+before\s+yesterday\b', 'relative', 'Relative phrase (2 day ago)',         'day before yesterday',  'relative_cue', 'seed_dateparser_en', 0.97),
  ('\bday\s+after\s+tomorrow\b',   'relative', 'Relative phrase (in 2 day)',          'day after tomorrow',    'relative_cue', 'seed_dateparser_en', 0.97),

  -- ── dateparser English: DEICTIC (CLDR base en.yaml today/yesterday/tomorrow/tonight/now) ──
  ('\btoday\b',               'relative', 'Deictic: today',                           'today',                 'relative_cue', 'seed_dateparser_en', 0.97),
  ('\byesterday\b',           'relative', 'Deictic: yesterday',                       'yesterday',             'relative_cue', 'seed_dateparser_en', 0.97),
  ('\btomorrow\b',            'relative', 'Deictic: tomorrow',                        'tomorrow',              'relative_cue', 'seed_dateparser_en', 0.97),
  ('\btonight\b',             'relative', 'Deictic: tonight',                         'tonight',               'relative_cue', 'seed_dateparser_en', 0.95),
  ('\bnow\b',                 'relative', 'Deictic: now',                             'now',                   'relative_cue', 'seed_dateparser_en', 0.80),

  -- ── HeidelTime English: reThisNextLast (last/past/next/latest/current/this/previous) ──
  ('\blast\b',                'relative', 'Relative indicator: last (HeidelTime reThisNextLast)',     'last Tuesday',  'relative_cue', 'seed_heideltime', 0.92),
  ('\bnext\b',                'relative', 'Relative indicator: next (HeidelTime reThisNextLast)',     'next month',    'relative_cue', 'seed_heideltime', 0.92),
  ('\bthis\b',                'relative', 'Relative indicator: this (HeidelTime reThisNextLast)',     'this week',     'relative_cue', 'seed_heideltime', 0.75),
  ('\bpast\b',                'relative', 'Relative indicator: past (HeidelTime reThisNextLast)',     'the past week', 'relative_cue', 'seed_heideltime', 0.78),
  ('\bprevious\b',            'relative', 'Relative indicator: previous (HeidelTime reThisNextLast)', 'previous year', 'relative_cue', 'seed_heideltime', 0.82),
  ('\bcurrent\b',             'relative', 'Relative indicator: current (HeidelTime reThisNextLast)',  'current month', 'relative_cue', 'seed_heideltime', 0.75),
  ('\blatest\b',              'relative', 'Relative indicator: latest (HeidelTime reThisNextLast)',   'latest week',   'relative_cue', 'seed_heideltime', 0.70),

  -- ── HeidelTime / TIMEX3 deictic relative indicators (recent / upcoming / coming) ──
  ('\brecent\b',              'relative', 'Relative indicator: recent (TIMEX3 deictic)',  'recent days',     'relative_cue', 'seed_heideltime', 0.75),
  ('\bupcoming\b',            'relative', 'Relative indicator: upcoming (TIMEX3 deictic)', 'upcoming week',  'relative_cue', 'seed_heideltime', 0.78),
  ('\bcoming\b',              'relative', 'Relative indicator: coming (TIMEX3 deictic)',   'coming month',   'relative_cue', 'seed_heideltime', 0.75),
  ('\bearlier\b',             'relative', 'Relative indicator: earlier (HeidelTime rePartWords late/later/early/earlier)', 'three days earlier', 'relative_cue', 'seed_heideltime', 0.82),

  -- ── Bare temporal UNIT words (dateparser en.yaml units) — "next week"/"last month"
  --     anchor relative; a bare unit alongside a relative indicator confirms relative. ──
  ('\bweek\b',                'relative', 'Temporal unit: week (dateparser en units)',  'next week',   'relative_cue', 'seed_dateparser_en', 0.55),
  ('\bmonth\b',               'relative', 'Temporal unit: month (dateparser en units)', 'last month',  'relative_cue', 'seed_dateparser_en', 0.55),
  ('\byear\b',                'relative', 'Temporal unit: year (dateparser en units)',  'this year',   'relative_cue', 'seed_dateparser_en', 0.55)
ON CONFLICT (pattern_regex, anchor_type) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- Create the table in each tenant schema (search_path has NO public at runtime) and seed it
-- from public. Mirrors 091's fan-out. Idempotent: ON CONFLICT DO NOTHING.

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
            CREATE TABLE IF NOT EXISTS %I.temporal_patterns (
                id                SERIAL PRIMARY KEY,
                pattern_regex     VARCHAR(1024) NOT NULL,
                anchor_type       VARCHAR(32)  NOT NULL DEFAULT 'relative',
                frequency         INT   DEFAULT 0,
                confirmed_count   INT   DEFAULT 0,
                rejected_count    INT   DEFAULT 0,
                correction_count  INT   DEFAULT 0,
                global_confidence FLOAT DEFAULT 0.5,
                description       TEXT,
                example_text      TEXT,
                category          VARCHAR(64),
                source            VARCHAR(64),
                is_active         BOOLEAN DEFAULT true,
                archived_at       TIMESTAMP,
                created_at        TIMESTAMP DEFAULT NOW(),
                updated_at        TIMESTAMP DEFAULT NOW(),
                last_matched_at   TIMESTAMP,
                CONSTRAINT chk_temporal_anchor_type_%s
                    CHECK (anchor_type IN ('relative', 'absolute_no_year', 'explicit_year')),
                UNIQUE (pattern_regex, anchor_type)
            )
        $ddl$, _schema, md5(_schema));  -- md5 suffix keeps the CHECK constraint name unique per schema

        EXECUTE format($ix1$
            CREATE INDEX IF NOT EXISTS idx_temporal_patterns_active
                ON %I.temporal_patterns(is_active, anchor_type)
        $ix1$, _schema);
        EXECUTE format($ix2$
            CREATE INDEX IF NOT EXISTS idx_temporal_patterns_category
                ON %I.temporal_patterns(category)
        $ix2$, _schema);

        -- ---- seed from public (template) ----
        EXECUTE format($seed$
            INSERT INTO %I.temporal_patterns
                (pattern_regex, anchor_type, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 category, source, is_active, archived_at, last_matched_at)
            SELECT pattern_regex, anchor_type, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   category, source, is_active, archived_at, last_matched_at
            FROM public.temporal_patterns
            ON CONFLICT (pattern_regex, anchor_type) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 103: temporal_patterns created + seeded into %', _schema;
    END LOOP;
END $$;
