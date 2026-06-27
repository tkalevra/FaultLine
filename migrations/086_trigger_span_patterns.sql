-- Migration 086: trigger-span detection patterns (category='trigger')
-- Date: 2026-06-14
-- Purpose: bolt the trigger→GLiNER2 detection layer to the DB.
--          See DEV/DESIGN-trigger-span-gliner2-extraction.md.
--
-- A `category='trigger'` row's pattern_regex is a FACT SIGNAL (date, issue/event verb,
-- "my X is", acquisition verb, "by the way" lead-in). It does NOT map to a rel_type — it
-- marks a sentence as fact-bearing so extract_rewrite hands that SPAN to GLiNER2's relation
-- extractor (the strong structured extractor). rel_type is the sentinel '__gliner2_span__'
-- (satisfies NOT NULL + UNIQUE(pattern_regex, rel_type); never used as an actual relation).
--
-- PITFALL 11: triggers are SEGMENTATION regexes, NOT GLiNER2 labels — they change WHERE
-- GLiNER2 looks, never WHAT it scores. Grow this set freely.
--
-- Idempotent: INSERT ... ON CONFLICT DO NOTHING. Seeds public (future tenants copy it via
-- schema_manager) then fans out to existing tenant schemas.

-- ── 1. Seed public (the template / seed source) ─────────────────────────────
INSERT INTO public.extraction_patterns
    (pattern_regex, rel_type, description, example_text, category, source, global_confidence)
VALUES
  ('\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b', '__gliner2_span__',
   'numeric date signal', 'I had an issue on 3/22', 'trigger', 'bootstrap', 0.6),
  ('\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}',
   '__gliner2_span__', 'month-name date signal', 'serviced on March 15th', 'trigger', 'bootstrap', 0.6),
  ('\b\d{4}-\d{2}-\d{2}\b', '__gliner2_span__',
   'ISO date signal', 'on 2023-03-22', 'trigger', 'bootstrap', 0.6),
  ('\b(?:issue|problem|trouble|error|fault|broke|broken|failed|failure)\b', '__gliner2_span__',
   'problem/event signal', 'I had an issue with my GPS', 'trigger', 'bootstrap', 0.6),
  ('\b(?:got|bought|purchased|acquired|installed|serviced|repaired|replaced|fixed|upgraded)\b',
   '__gliner2_span__', 'acquisition/action signal', 'I got my car serviced', 'trigger', 'bootstrap', 0.6),
  ('\b(?:started|joined|moved|adopted|signed up|enrolled|registered|switched)\b', '__gliner2_span__',
   'life-event signal', 'I moved to Toronto', 'trigger', 'bootstrap', 0.6),
  ('\bmy\s+\w+\s+(?:is|was|are|were|has|have)\b', '__gliner2_span__',
   'possessive-attribute signal', 'my car is a Subaru', 'trigger', 'bootstrap', 0.6),
  ('\b(?:named|called|goes by)\b', '__gliner2_span__',
   'naming signal', 'my son is named Sampson', 'trigger', 'bootstrap', 0.6),
  ('\bby the way\b', '__gliner2_span__',
   'in-passing fact lead-in', 'by the way, I recently...', 'trigger', 'bootstrap', 0.55),
  ('\b(?:bought|sold|rented|leased|owns?|own)\b', '__gliner2_span__',
   'possession/transaction signal', 'I own a Subaru Outback', 'trigger', 'bootstrap', 0.6)
ON CONFLICT (pattern_regex, rel_type) DO NOTHING;

-- ── 2. Fan out to existing tenant schemas (copy from public) ────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format($seed$
            INSERT INTO %I.extraction_patterns
                (pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 category, source, is_active, archived_at, last_matched_at)
            SELECT pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   category, source, is_active, archived_at, last_matched_at
            FROM public.extraction_patterns
            WHERE category = 'trigger'
            ON CONFLICT (pattern_regex, rel_type) DO NOTHING
        $seed$, _schema);
        RAISE NOTICE 'Migration 086: seeded trigger patterns into %', _schema;
    END LOOP;
END $$;
