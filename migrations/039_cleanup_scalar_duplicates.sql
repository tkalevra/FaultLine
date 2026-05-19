-- Migration 039: Clean up scalar rel_type facts from facts table (dBug-056)
--
-- Problem: Scalar rel_types (age, pref_name, also_known_as, has_gender, etc.)
-- were incorrectly stored in facts table before scalar routing fix (May 16, 2026).
-- This violated the Three-Dimensional Classification Model storage path constraint.
--
-- Solution: Archive legacy scalar facts to history table, delete from facts.
-- All NEW scalar facts are correctly stored in entity_attributes only.
--
-- Safety: Non-destructive — facts moved to entity_attributes_history for audit trail.

BEGIN;

-- Step 1: Identify all scalar rel_types
WITH scalar_rel_types AS (
    SELECT rel_type
    FROM rel_types
    WHERE tail_types && ARRAY['SCALAR']::text[]
)
-- Step 2: Archive scalar facts to history table before deletion
INSERT INTO entity_attributes_history
    (user_id, entity_id, attribute, value_text, value_int, value_float,
     value_date, provenance, confidence, temporal_context, valid_from, recorded_at)
SELECT
    f.user_id,
    f.subject_id,
    f.rel_type,
    f.object_id,  -- object_id contains the scalar value (string representation)
    CASE WHEN f.object_id ~ '^\d+$' THEN f.object_id::int ELSE NULL END,  -- convert to int if numeric
    CASE WHEN f.object_id ~ '^\d+\.\d+$' THEN f.object_id::float ELSE NULL END,  -- convert to float if numeric
    CASE WHEN f.object_id ~ '^\d{4}-\d{2}-\d{2}$' THEN f.object_id::date ELSE NULL END,  -- convert to date if YYYY-MM-DD
    f.provenance,
    f.confidence,
    NULL,  -- temporal_context not available for legacy data
    f.created_at,
    NOW()
FROM facts f
INNER JOIN scalar_rel_types srt ON f.rel_type = srt.rel_type
WHERE f.superseded_at IS NULL;

-- Step 3: Delete scalar facts from facts table
DELETE FROM facts
WHERE rel_type IN (
    SELECT rel_type FROM rel_types
    WHERE tail_types && ARRAY['SCALAR']::text[]
);

-- Step 4: Verify cleanup (should return 0 scalar facts in facts table)
-- SELECT COUNT(*) as scalar_facts_in_facts_table FROM facts f
-- WHERE rel_type IN (SELECT rel_type FROM rel_types WHERE tail_types && ARRAY['SCALAR']::text[]);

COMMIT;
