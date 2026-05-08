-- Migration 017: Schema consistency fixes
-- 1. Rename allowed_head / allowed_tail → head_types / tail_types
-- 2. Seed rel_types missing from the initial seeds but referenced throughout the codebase
-- 3. Fix staged_facts column types (UUID → TEXT) to match facts table

-- ============================================================================
-- Part 1: Rename allowed_head → head_types, allowed_tail → tail_types
-- ============================================================================
DO $$
BEGIN
    -- head_types
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'allowed_head'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'head_types'
    ) THEN
        ALTER TABLE rel_types RENAME COLUMN allowed_head TO head_types;
    END IF;

    -- tail_types
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'allowed_tail'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'tail_types'
    ) THEN
        ALTER TABLE rel_types RENAME COLUMN allowed_tail TO tail_types;
    END IF;
END $$;

-- Ensure columns exist even if the old names were never present
ALTER TABLE rel_types ADD COLUMN IF NOT EXISTS head_types TEXT[];
ALTER TABLE rel_types ADD COLUMN IF NOT EXISTS tail_types TEXT[];

-- ============================================================================
-- Part 2: Seed missing rel_types
-- These are referenced throughout the codebase (_CLASS_B_REL_TYPES,
-- _SCALAR_REL_TYPES, _SENSITIVE_RELS, _BASELINE_RELS, _infer_category,
-- migration 013 category backfill) but were never inserted into rel_types.
-- ============================================================================
INSERT INTO rel_types (rel_type, label, engine_generated, confidence, source,
                        correction_behavior, category, head_types, tail_types)
VALUES
    ('lives_at',   'Lives At',   false, 1.0, 'builtin', 'supersede',
     'location', ARRAY['Person'], ARRAY['Location']),
    ('located_at', 'Located At', false, 1.0, 'builtin', 'supersede',
     'location', ARRAY['ANY'], ARRAY['Location']),
    ('has_pet',    'Has Pet',    false, 1.0, 'builtin', 'supersede',
     'pets',     ARRAY['Person'], ARRAY['Animal']),
    ('height',     'Height',     false, 1.0, 'builtin', 'supersede',
     'physical', ARRAY['Person'], ARRAY['SCALAR']),
    ('weight',     'Weight',     false, 1.0, 'builtin', 'supersede',
     'physical', ARRAY['Person'], ARRAY['SCALAR'])
ON CONFLICT (rel_type) DO UPDATE SET
    category        = EXCLUDED.category,
    head_types      = EXCLUDED.head_types,
    tail_types      = EXCLUDED.tail_types,
    source          = EXCLUDED.source,
    correction_behavior = EXCLUDED.correction_behavior;

-- Backfill category for any engine-generated types that have NULL category
UPDATE rel_types
SET category = CASE
    WHEN rel_type IN ('lives_at','lives_in','located_in','located_at','address','born_in') THEN 'location'
    WHEN rel_type IN ('parent_of','child_of','spouse','sibling_of') THEN 'family'
    WHEN rel_type IN ('works_for','occupation','educated_at') THEN 'work'
    WHEN rel_type IN ('height','weight','has_gender') THEN 'physical'
    WHEN rel_type IN ('born_on','age','anniversary_on','met_on') THEN 'temporal'
    WHEN rel_type IN ('has_pet') THEN 'pets'
    WHEN rel_type IN ('also_known_as','pref_name','same_as','is_a','instance_of','subclass_of') THEN 'identity'
    ELSE category
END
WHERE category IS NULL;

-- ============================================================================
-- Part 3: Fix staged_facts column types (UUID → TEXT)
-- The facts table uses TEXT for user_id/subject_id/object_id. staged_facts
-- must match — otherwise INSERTs fail because entity names (resolved UUID
-- v5 surrogates as strings, or "anonymous" for default user) cannot always
-- be cast to PostgreSQL UUID type.
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staged_facts' AND column_name = 'user_id'
          AND data_type = 'uuid'
    ) THEN
        ALTER TABLE staged_facts ALTER COLUMN user_id TYPE TEXT;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staged_facts' AND column_name = 'subject_id'
          AND data_type = 'uuid'
    ) THEN
        ALTER TABLE staged_facts ALTER COLUMN subject_id TYPE TEXT;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'staged_facts' AND column_name = 'object_id'
          AND data_type = 'uuid'
    ) THEN
        ALTER TABLE staged_facts ALTER COLUMN object_id TYPE TEXT;
    END IF;
END $$;

-- Update the stored procedure to remove the now-unnecessary ::text casts
-- (keeping them is harmless, but removing them makes the intent clearer)
CREATE OR REPLACE FUNCTION promote_staged_fact(p_staged_fact_id BIGINT)
RETURNS VOID AS $$
DECLARE
    v_fact RECORD;
    v_confirmation_source TEXT;
    v_distinct_session_count INTEGER;
BEGIN
    SELECT * INTO v_fact FROM staged_facts WHERE id = p_staged_fact_id;

    IF v_fact IS NULL THEN
        RAISE NOTICE 'Staged fact % not found', p_staged_fact_id;
        RETURN;
    END IF;

    IF v_fact.promoted_at IS NOT NULL THEN
        RETURN;
    END IF;

    v_confirmation_source := v_fact.confirmation_source;

    IF v_confirmation_source = 'inference_chain' THEN
        RAISE NOTICE 'inference_chain facts never promoted: staged_fact_id %', p_staged_fact_id;
        RETURN;
    ELSIF v_confirmation_source = 'user_explicit' THEN
        IF v_fact.confirmed_count < 1 THEN
            RETURN;
        END IF;
    ELSIF v_confirmation_source = 'llm_repeat' THEN
        SELECT COUNT(DISTINCT session_id) INTO v_distinct_session_count
        FROM staged_fact_confirmations
        WHERE staged_fact_id = p_staged_fact_id;

        IF COALESCE(v_distinct_session_count, 0) < 5 THEN
            RETURN;
        END IF;
    END IF;

    INSERT INTO facts (user_id, subject_id, object_id, rel_type, provenance,
                       fact_provenance, fact_class, confidence, confirmed_count,
                       valid_from, last_seen_at, recorded_at, qdrant_synced)
    SELECT v_fact.user_id, v_fact.subject_id, v_fact.object_id,
           v_fact.rel_type, v_fact.provenance, 'llm_promoted', v_fact.fact_class,
           v_fact.confidence, v_fact.confirmed_count, now(), now(), now(), false
    ON CONFLICT (user_id, subject_id, object_id, rel_type) DO UPDATE
    SET confirmed_count = facts.confirmed_count + 1,
        last_seen_at    = now(),
        qdrant_synced   = false;

    UPDATE staged_facts SET promoted_at = now() WHERE id = p_staged_fact_id;
END;
$$ LANGUAGE plpgsql;
