-- Migration: intent_classes
-- Date: 2026-05-29
-- Purpose: Metadata-driven intent class definitions for GLiNER2 semantic enrichment
-- Respects dprompt-152: DB-driven intent descriptions improve zero-shot classification

-- Table: intent_classes
-- Stores semantic descriptions of intent classes
-- Used by /classify-intent endpoint to build GLiNER2 labels
-- Enables customization: admins/re_embedder can refine descriptions over time

CREATE TABLE IF NOT EXISTS intent_classes (
    id SERIAL PRIMARY KEY,

    -- Intent class name (QUERY, RETRACTION, CORRECTION, STATEMENT)
    intent_name VARCHAR(50) NOT NULL UNIQUE,

    -- Human-readable semantic description for GLiNER2
    -- Used as label passed to GLiNER2.classify_text()
    -- Quality of description directly impacts classification accuracy
    description TEXT NOT NULL,

    -- Priority for tie-breaking (higher = prefer this intent)
    -- Used by re_embedder when evaluating novel intents
    priority INT DEFAULT 100,

    -- Version control: track when description was last refined
    version INT DEFAULT 1,
    refined_at TIMESTAMP DEFAULT now(),

    -- Lifecycle: admins can soft-delete intents
    is_active BOOLEAN DEFAULT true,

    -- Metadata
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),

    -- Audit: who refined the description (for future use)
    refined_by VARCHAR(255) DEFAULT 'bootstrap'
);

-- Indexes for fast lookup
CREATE INDEX IF NOT EXISTS idx_intent_classes_name ON intent_classes (intent_name);
CREATE INDEX IF NOT EXISTS idx_intent_classes_active ON intent_classes (is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_intent_classes_priority ON intent_classes (priority DESC) WHERE is_active = true;

-- Bootstrap: Populate intent classes with semantic descriptions
-- These descriptions are optimized for GLiNER2 zero-shot classification
-- Based on analysis: better descriptions → higher confidence + accuracy

INSERT INTO intent_classes (intent_name, description, priority, refined_by)
VALUES
    (
        'QUERY',
        'User is asking a question to retrieve information',
        100,
        'bootstrap'
    ),
    (
        'RETRACTION',
        'User wants to remove or forget information',
        100,
        'bootstrap'
    ),
    (
        'CORRECTION',
        'User is correcting or updating previous information',
        100,
        'bootstrap'
    ),
    (
        'STATEMENT',
        'User is providing new information or facts',
        100,
        'bootstrap'
    )
ON CONFLICT (intent_name) DO UPDATE
SET
    description = EXCLUDED.description,
    priority = EXCLUDED.priority,
    version = intent_classes.version + 1,
    refined_at = NOW(),
    updated_at = NOW()
WHERE intent_classes.refined_by != 'user';  -- Don't override user customizations

-- Verification: Ensure all 4 intents are present and active
DO $$
DECLARE
    missing_count INT;
BEGIN
    SELECT COUNT(*) INTO missing_count
    FROM (
        VALUES ('QUERY'), ('RETRACTION'), ('CORRECTION'), ('STATEMENT')
    ) AS required_intents(intent)
    WHERE intent NOT IN (
        SELECT intent_name FROM intent_classes WHERE is_active = true
    );

    IF missing_count > 0 THEN
        RAISE WARNING 'Migration 053: % intent classes missing or inactive', missing_count;
    END IF;
END $$;