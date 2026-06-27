-- Migration 104: temporal_patterns — seed the FORMAL-ABSOLUTE class (gate coverage)
-- Date: 2026-06-20
--
-- WHY
-- ---
-- Migration 103 moved the RELATIVE-cue inventory into per-tenant DB rows so the
-- relative-vs-absolute SPAN classification is data-driven + growable. It deliberately
-- left the closed FORMAL ABSOLUTE class (month names / numeric date shapes / 4-digit
-- year) in code, because those were only ever consumed by the span COLLECTOR + the
-- absolute branch of extract_event_date — not by a per-tenant resolver.
--
-- We now add a LATENCY GATE in front of the whole date pipeline (spaCy DATE NER +
-- dateparser): before any of that runs, a CHEAP single combined-regex precheck asks
-- "does this turn match ANY active temporal_patterns row?". A no-cue turn ("my name is
-- Alexander") skips the entire date engine. For that gate to be COMPLETE it must also
-- recognize ABSOLUTE dates that carry NO relative cue ("January 17th", "3/22", "2023") —
-- otherwise the gate would skip a real absolute date. So this migration SEEDS the formal
-- absolute surface forms as DB rows too, making the table the single source of truth the
-- combined gate compiles. The actual relative-vs-absolute CLASSIFICATION + the year-anchor
-- math STAY in code (closed, deterministic) — these rows feed the GATE, not the classifier.
--
-- WHAT THIS SEEDS (anchor_type ∈ {absolute_no_year, explicit_year}, category='formal_absolute')
--   * 'explicit_year'    — a 4-digit year token (mirrors _EXPLICIT_YEAR_RE).
--   * 'absolute_no_year' — the 12 month names (+ common 3-letter abbreviations) and the
--                          numeric date shapes (M/D, M/D/Y, ISO Y/M/D — mirrors
--                          linguistics._NUMERIC_DATE_PATTERNS).
-- These are a CLOSED FORMAL class (month names + digit shapes), legitimate as data exactly
-- like the in-code month tuple. NOT an open-ended word-list; growth does not touch them.
--
-- PER-TENANT: runtime search_path EXCLUDES public — public rows are template/seed only.
-- New tenants inherit via the provisioning seeder (INSERT ... SELECT FROM public.temporal_patterns);
-- EXISTING tenants are backfilled by the DO loop below (mirrors 103). Idempotent: ON CONFLICT
-- DO NOTHING (UNIQUE (pattern_regex, anchor_type)). Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL.

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the FORMAL-ABSOLUTE class
-- ============================================================================
-- pattern_regex is a word-boundary regex over the lowercased turn surface (the overlay reader
-- compiles it case-insensitively). The combined GATE matcher ORs all active rows together.

INSERT INTO public.temporal_patterns
    (pattern_regex, anchor_type, description, example_text, category, source, global_confidence)
VALUES
  -- ── Explicit 4-digit year (mirrors linguistics._EXPLICIT_YEAR_RE) ──────────────
  ('\b(?:19|20)\d{2}\b', 'explicit_year', 'Explicit 4-digit year token', 'in 2023', 'formal_absolute', 'seed_formal_absolute', 0.95),

  -- ── Numeric date shapes (mirror linguistics._NUMERIC_DATE_PATTERNS) ────────────
  ('\b\d{4}/\d{1,2}/\d{1,2}\b',        'absolute_no_year', 'Numeric date (year-first Y/M/D)', '2023/04/10', 'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b', 'absolute_no_year', 'Numeric date (M/D or M/D/Y)',     '3/22',       'formal_absolute', 'seed_formal_absolute', 0.92),

  -- ── 12 month names + common abbreviations (closed formal class; mirrors the in-code month tuple) ──
  ('\bjanuary\b',   'absolute_no_year', 'Month name: January',   'January 17th', 'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bfebruary\b',  'absolute_no_year', 'Month name: February',  'February 3',   'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bmarch\b',     'absolute_no_year', 'Month name: March',     'March 1',      'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bapril\b',     'absolute_no_year', 'Month name: April',     'April 10',     'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bmay\b',       'absolute_no_year', 'Month name: May',       'May 5',        'formal_absolute', 'seed_formal_absolute', 0.70),
  ('\bjune\b',      'absolute_no_year', 'Month name: June',      'June 21',      'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bjuly\b',      'absolute_no_year', 'Month name: July',      'July 4',       'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\baugust\b',    'absolute_no_year', 'Month name: August',    'August 30',    'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bseptember\b', 'absolute_no_year', 'Month name: September', 'September 9',   'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\boctober\b',   'absolute_no_year', 'Month name: October',   'October 12',   'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bnovember\b',  'absolute_no_year', 'Month name: November',  'November 2',   'formal_absolute', 'seed_formal_absolute', 0.95),
  ('\bdecember\b',  'absolute_no_year', 'Month name: December',  'December 25',   'formal_absolute', 'seed_formal_absolute', 0.95),
  -- 3-letter abbreviations (\.? tolerates "Jan." ). "may"/"march" need no abbrev row (full names cover).
  ('\bjan\.?\b',  'absolute_no_year', 'Month abbrev: Jan',  'Jan 17',  'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bfeb\.?\b',  'absolute_no_year', 'Month abbrev: Feb',  'Feb 3',   'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bmar\.?\b',  'absolute_no_year', 'Month abbrev: Mar',  'Mar 1',   'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bapr\.?\b',  'absolute_no_year', 'Month abbrev: Apr',  'Apr 10',  'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bjun\.?\b',  'absolute_no_year', 'Month abbrev: Jun',  'Jun 21',  'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bjul\.?\b',  'absolute_no_year', 'Month abbrev: Jul',  'Jul 4',   'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\baug\.?\b',  'absolute_no_year', 'Month abbrev: Aug',  'Aug 30',  'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bsep\.?\b',  'absolute_no_year', 'Month abbrev: Sep',  'Sep 9',   'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bsept\.?\b', 'absolute_no_year', 'Month abbrev: Sept', 'Sept 9',  'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\boct\.?\b',  'absolute_no_year', 'Month abbrev: Oct',  'Oct 12',  'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bnov\.?\b',  'absolute_no_year', 'Month abbrev: Nov',  'Nov 2',   'formal_absolute', 'seed_formal_absolute', 0.85),
  ('\bdec\.?\b',  'absolute_no_year', 'Month abbrev: Dec',  'Dec 25',  'formal_absolute', 'seed_formal_absolute', 0.85)
ON CONFLICT (pattern_regex, anchor_type) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The temporal_patterns table already exists in each tenant (migration 103). Seed the new
-- formal_absolute rows from public. Idempotent: ON CONFLICT DO NOTHING.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        BEGIN
            EXECUTE format($seed$
                INSERT INTO %I.temporal_patterns
                    (pattern_regex, anchor_type, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     category, source, is_active, archived_at, last_matched_at)
                SELECT pattern_regex, anchor_type, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       category, source, is_active, archived_at, last_matched_at
                FROM public.temporal_patterns
                WHERE category = 'formal_absolute'
                ON CONFLICT (pattern_regex, anchor_type) DO NOTHING
            $seed$, _schema);
            RAISE NOTICE 'Migration 104: formal_absolute temporal_patterns seeded into %', _schema;
        EXCEPTION WHEN undefined_table THEN
            -- Tenant predates migration 103 (no temporal_patterns table) — skip; 103 backfill
            -- will create+seed it (it copies ALL public rows, including these). Fail-safe.
            RAISE NOTICE 'Migration 104: % has no temporal_patterns (pre-103) — skipped', _schema;
        END;
    END LOOP;
END $$;
