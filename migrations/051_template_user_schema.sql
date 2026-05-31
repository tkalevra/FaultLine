-- Migration: Template schema structure for new user schemas
-- Date: 2026-05-27
-- Purpose: Define the SQL DDL for creating new user schemas
-- NOTE: This file serves as a template. The actual schema creation happens
--       via schema_manager.py, which reads this file and substitutes {schema_name}

-- SCHEMA VALIDATION CHECKLIST (schema_manager.py MUST verify ALL of these exist):
--
-- facts table REQUIRED COLUMNS: valid_from, valid_until, fact_class, fact_provenance,
--   unified_confidence, superseded_at, archived_at, storage_type, is_hierarchy_rel,
--   taxonomies, rel_type_definition
--
-- staged_facts table REQUIRED COLUMNS: fact_class, fact_provenance, unified_confidence,
--   storage_type, is_hierarchy_rel, taxonomies, rel_type_definition
--
-- entity_attributes table REQUIRED COLUMNS: user_id, entity_id, attribute, value_text
--
-- REQUIRED TABLES: facts, staged_facts, entities, entity_aliases, entity_attributes,
--   rel_types, entity_taxonomies, ontology_evaluations, negation_patterns,
--   intent_confidence_feedback, retraction_signals, pending_types,
--   entity_name_conflicts, retraction_outcomes
--
-- If ANY of these are missing or have wrong type:
--   1. schema_manager.py MUST log ERROR with full details
--   2. Set status='failed', error_message=[exact error]
--   3. NEVER set status='ready'
--   4. Fail loud per CLAUDE.md constraint #3

-- USER SCHEMA TEMPLATE: Replace {schema_name} with actual schema name (e.g., faultline_christopher)
-- This template is applied when provisioning a new user schema

-- Create schema
CREATE SCHEMA IF NOT EXISTS {schema_name};

-- Facts table: relationship facts
CREATE TABLE IF NOT EXISTS facts (
    id SERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    provenance TEXT,
    fact_provenance TEXT,
    fact_class TEXT DEFAULT 'B',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    qdrant_synced BOOLEAN NOT NULL DEFAULT false,
    superseded_at TIMESTAMPTZ,
    confidence FLOAT NOT NULL DEFAULT 0.8,
    unified_confidence DOUBLE PRECISION DEFAULT 1.0,
    confirmed_count INT DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    contradicted_by TEXT,
    is_preferred_label BOOLEAN DEFAULT false,
    rel_type_definition TEXT DEFAULT '',
    storage_type TEXT,
    is_hierarchy_rel BOOLEAN DEFAULT false,
    taxonomies TEXT[] DEFAULT '{}',
    archived_at TIMESTAMPTZ,
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    UNIQUE(subject_id, object_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_facts_pair
    ON facts (subject_id, object_id);
CREATE INDEX IF NOT EXISTS idx_facts_rel_type
    ON facts (rel_type);
CREATE INDEX IF NOT EXISTS idx_facts_unsynced
    ON facts (qdrant_synced)
    WHERE qdrant_synced = false;
CREATE INDEX IF NOT EXISTS idx_facts_confidence
    ON facts (confidence DESC);
CREATE INDEX IF NOT EXISTS idx_facts_unified_confidence
    ON facts (unified_confidence DESC)
    WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_taxonomies
    ON facts USING GIN(taxonomies);

-- Staged facts table: lower-confidence facts awaiting promotion
CREATE TABLE IF NOT EXISTS staged_facts (
    id SERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    fact_class TEXT DEFAULT 'B',
    provenance TEXT,
    fact_provenance TEXT,
    confidence FLOAT NOT NULL DEFAULT 0.6,
    unified_confidence DOUBLE PRECISION DEFAULT 0.8,
    confirmed_count INT DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    promoted_at TIMESTAMPTZ,
    qdrant_synced BOOLEAN NOT NULL DEFAULT false,
    rel_type_definition TEXT DEFAULT '',
    storage_type TEXT,
    is_hierarchy_rel BOOLEAN DEFAULT false,
    taxonomies TEXT[] DEFAULT '{}',
    UNIQUE(subject_id, object_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_staged_facts_pair
    ON staged_facts (subject_id, object_id);
CREATE INDEX IF NOT EXISTS idx_staged_facts_expires
    ON staged_facts (expires_at)
    WHERE promoted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_staged_facts_promoted
    ON staged_facts (promoted_at)
    WHERE promoted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_staged_facts_unified_confidence
    ON staged_facts (unified_confidence DESC);
CREATE INDEX IF NOT EXISTS idx_staged_facts_taxonomies
    ON staged_facts USING GIN(taxonomies);

-- Entities: canonical entity registry (per-user schema, no user_id needed — schema isolation is sufficient)
CREATE TABLE IF NOT EXISTS entities (
    id TEXT NOT NULL PRIMARY KEY,
    entity_type TEXT DEFAULT 'unknown',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entities_type
    ON entities (entity_type);

-- Entity aliases: preferred names and alternate names for entities
CREATE TABLE IF NOT EXISTS entity_aliases (
    id SERIAL PRIMARY KEY,
    entity_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    is_preferred BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    valid_from TIMESTAMP WITH TIME ZONE DEFAULT now(),
    valid_until TIMESTAMP WITH TIME ZONE,
    UNIQUE (entity_id, alias),
    FOREIGN KEY (entity_id) REFERENCES entities(id)
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity
    ON entity_aliases (entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_aliases_one_preferred
    ON entity_aliases (entity_id) WHERE is_preferred = true;
CREATE INDEX IF NOT EXISTS idx_entity_aliases_preferred
    ON entity_aliases (is_preferred)
    WHERE is_preferred = true;

-- Entity attributes: scalar facts (age, height, occupation, etc.)
-- Per-user schema: user_id is implicit through schema isolation, not stored
CREATE TABLE IF NOT EXISTS entity_attributes (
    user_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value_text TEXT,
    value_int INT,
    value_float FLOAT,
    value_date DATE,
    provenance TEXT,
    sensitivity VARCHAR(50),
    category VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    superseded_at TIMESTAMP WITH TIME ZONE,
    valid_until TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (entity_id, attribute),
    UNIQUE (entity_id, attribute),
    FOREIGN KEY (entity_id) REFERENCES entities(id)
);

CREATE INDEX IF NOT EXISTS idx_entity_attributes_user_id
    ON entity_attributes (user_id);
CREATE INDEX IF NOT EXISTS idx_entity_attributes_user_entity
    ON entity_attributes (user_id, entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_attributes_entity
    ON entity_attributes (entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_attributes_attribute
    ON entity_attributes (attribute);

-- Relationship type metadata (per-schema copy)
CREATE TABLE IF NOT EXISTS rel_types (
    rel_type TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    wikidata_pid TEXT,
    head_types TEXT[],
    tail_types TEXT[],
    engine_generated BOOLEAN NOT NULL DEFAULT false,
    confidence FLOAT NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    inverse_of TEXT,
    correction_behavior TEXT,
    source TEXT,
    category TEXT,
    is_symmetric BOOLEAN DEFAULT false,
    inverse_rel_type TEXT,
    is_leaf_only BOOLEAN DEFAULT false,
    is_hierarchy_rel BOOLEAN DEFAULT false,
    allows_leaf_rels TEXT[],
    storage_target TEXT,
    fact_class TEXT,
    natural_language TEXT,
    examples TEXT,
    value_distribution TEXT,
    approved_exceptions TEXT,
    anomaly_threshold FLOAT
);

CREATE INDEX IF NOT EXISTS idx_rel_types_engine_generated
    ON rel_types (engine_generated);
CREATE INDEX IF NOT EXISTS idx_rel_types_category
    ON rel_types (category);
CREATE INDEX IF NOT EXISTS idx_rel_types_is_hierarchy
    ON rel_types (is_hierarchy_rel);

-- Entity taxonomies: semantic groupings (per-schema copy)
CREATE TABLE IF NOT EXISTS entity_taxonomies (
    id SERIAL PRIMARY KEY,
    taxonomy_name TEXT NOT NULL UNIQUE,
    description TEXT,
    member_entity_types TEXT[] NOT NULL,
    rel_types_defining_group TEXT[],
    has_transitivity BOOLEAN DEFAULT false,
    transitive_rel_types TEXT[],
    is_hierarchical BOOLEAN DEFAULT false,
    parent_rel_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_taxonomies_name
    ON entity_taxonomies (taxonomy_name);

-- Ontology evaluations: novel relationship types awaiting approval (per-schema)
CREATE TABLE IF NOT EXISTS ontology_evaluations (
    id BIGSERIAL PRIMARY KEY,
    candidate_rel_type VARCHAR(128) NOT NULL,
    candidate_subject_type VARCHAR(64),
    candidate_object_type VARCHAR(64),
    first_text_snippet TEXT,
    extraction_confidence DOUBLE PRECISION DEFAULT 0.5,
    extraction_method VARCHAR(32) DEFAULT 'llm_extract',
    sample_subject_id TEXT,
    sample_object TEXT,
    occurrence_count INT DEFAULT 1,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    pattern_similarity DOUBLE PRECISION,
    best_fit_rel_type VARCHAR(128),
    best_fit_score DOUBLE PRECISION,
    re_embedder_decision VARCHAR(32),
    re_embedder_confidence DOUBLE PRECISION,
    decision_timestamp TIMESTAMP WITH TIME ZONE,
    decision_reason TEXT,
    created_rel_type VARCHAR(128),
    promoted_to_facts BOOLEAN DEFAULT false,
    llm_natural_language VARCHAR(500),
    llm_is_symmetric BOOLEAN,
    llm_inverse_rel_type VARCHAR(255),
    llm_category VARCHAR(100),
    llm_fact_class CHAR(1),
    llm_confidence DOUBLE PRECISION,
    llm_metadata_json JSONB,
    UNIQUE(candidate_rel_type, sample_subject_id, sample_object)
);

CREATE INDEX IF NOT EXISTS idx_ontology_eval_candidate
    ON ontology_evaluations (candidate_rel_type, occurrence_count DESC);
CREATE INDEX IF NOT EXISTS idx_ontology_eval_decision
    ON ontology_evaluations (re_embedder_decision, last_seen_at)
    WHERE re_embedder_decision IS NULL;

-- Negation patterns: linguistic patterns for detecting retractions (per-schema)
CREATE TABLE IF NOT EXISTS negation_patterns (
    id SERIAL PRIMARY KEY,
    pattern_text TEXT NOT NULL,
    negation_type VARCHAR(50) NOT NULL,
    learned_from VARCHAR(50) DEFAULT 'correction_feedback',
    confidence FLOAT DEFAULT 0.4,
    confirmed_count INT DEFAULT 0,
    contradicted_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    pattern_hash VARCHAR(16),
    UNIQUE(pattern_text, negation_type)
);

CREATE INDEX IF NOT EXISTS idx_negation_patterns_type
    ON negation_patterns (negation_type);
CREATE INDEX IF NOT EXISTS idx_negation_patterns_confidence
    ON negation_patterns (confidence DESC);
CREATE INDEX IF NOT EXISTS idx_negation_pattern_hash
    ON negation_patterns (pattern_hash);

-- Intent confidence feedback: adaptive confidence gating per user (per-schema)
CREATE TABLE IF NOT EXISTS intent_confidence_feedback (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    confidence_bin VARCHAR(10) NOT NULL,
    feedback_type VARCHAR(20) NOT NULL,
    count INT DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE (user_id, confidence_bin, feedback_type)
);

CREATE INDEX IF NOT EXISTS idx_intent_confidence_feedback_bin
    ON intent_confidence_feedback (confidence_bin);

-- Retraction signals: learned signals for improving intent classification (per-schema)
-- Growth engine: learned from successful retractions, strengthens GLiNER2 over time
CREATE TABLE IF NOT EXISTS retraction_signals (
    id SERIAL PRIMARY KEY,
    signal TEXT NOT NULL,
    signal_category VARCHAR(50) NOT NULL,
    language VARCHAR(5) NOT NULL DEFAULT 'en',
    priority INT DEFAULT 50,
    false_positive_rate FLOAT DEFAULT 0.0,
    false_negative_rate FLOAT DEFAULT 0.0,
    notes TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(signal, language)
);

CREATE INDEX IF NOT EXISTS idx_retraction_signals_priority
    ON retraction_signals(language, priority DESC);

-- Pending types: novel entity types awaiting classification
CREATE TABLE IF NOT EXISTS pending_types (
    id SERIAL PRIMARY KEY,
    rel_type TEXT NOT NULL,
    subject_id TEXT,
    object_id TEXT,
    flagged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (rel_type)
);

CREATE INDEX IF NOT EXISTS idx_pending_types_rel_type
    ON pending_types (rel_type);

-- Entity name conflicts: pending name collision disputes (per-schema)
CREATE TABLE IF NOT EXISTS entity_name_conflicts (
    id SERIAL PRIMARY KEY,
    alias TEXT NOT NULL,
    entity_id_a TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_id_b TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | resolved
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (alias)
);

CREATE INDEX IF NOT EXISTS idx_entity_name_conflicts_status
    ON entity_name_conflicts (status);

-- Triggers for lowercase normalization
-- Retraction outcomes: tracking retraction detection and execution (for learning)
CREATE TABLE IF NOT EXISTS retraction_outcomes (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255),
    original_message TEXT NOT NULL,
    detected_as_retraction BOOLEAN,
    retraction_method VARCHAR(50),
    detected_confidence FLOAT,
    extracted_subject VARCHAR(255),
    extracted_rel_type VARCHAR(255),
    extracted_old_value TEXT,
    actually_retracted BOOLEAN,
    was_correct BOOLEAN,
    created_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retraction_outcomes_user
    ON retraction_outcomes(user_id);
CREATE INDEX IF NOT EXISTS idx_retraction_outcomes_was_correct
    ON retraction_outcomes(was_correct);

CREATE OR REPLACE FUNCTION lowercase_facts()
RETURNS TRIGGER AS $$
BEGIN
    NEW.subject_id = LOWER(TRIM(NEW.subject_id));
    NEW.object_id = LOWER(TRIM(NEW.object_id));
    NEW.rel_type = LOWER(TRIM(NEW.rel_type));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lowercase_facts_before_insert ON facts;
CREATE TRIGGER lowercase_facts_before_insert
    BEFORE INSERT ON facts
    FOR EACH ROW EXECUTE FUNCTION lowercase_facts();

DROP TRIGGER IF EXISTS lowercase_facts_before_update ON facts;
CREATE TRIGGER lowercase_facts_before_update
    BEFORE UPDATE ON facts
    FOR EACH ROW EXECUTE FUNCTION lowercase_facts();

CREATE OR REPLACE FUNCTION lowercase_staged_facts()
RETURNS TRIGGER AS $$
BEGIN
    NEW.subject_id = LOWER(TRIM(NEW.subject_id));
    NEW.object_id = LOWER(TRIM(NEW.object_id));
    NEW.rel_type = LOWER(TRIM(NEW.rel_type));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lowercase_staged_facts_before_insert ON staged_facts;
CREATE TRIGGER lowercase_staged_facts_before_insert
    BEFORE INSERT ON staged_facts
    FOR EACH ROW EXECUTE FUNCTION lowercase_staged_facts();

DROP TRIGGER IF EXISTS lowercase_staged_facts_before_update ON staged_facts;
CREATE TRIGGER lowercase_staged_facts_before_update
    BEFORE UPDATE ON staged_facts
    FOR EACH ROW EXECUTE FUNCTION lowercase_staged_facts();

CREATE OR REPLACE FUNCTION lowercase_rel_types()
RETURNS TRIGGER AS $$
BEGIN
    NEW.rel_type = LOWER(TRIM(NEW.rel_type));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lowercase_rel_types_before_insert ON rel_types;
CREATE TRIGGER lowercase_rel_types_before_insert
    BEFORE INSERT ON rel_types
    FOR EACH ROW EXECUTE FUNCTION lowercase_rel_types();

DROP TRIGGER IF EXISTS lowercase_rel_types_before_update ON rel_types;
CREATE TRIGGER lowercase_rel_types_before_update
    BEFORE UPDATE ON rel_types
    FOR EACH ROW EXECUTE FUNCTION lowercase_rel_types();
