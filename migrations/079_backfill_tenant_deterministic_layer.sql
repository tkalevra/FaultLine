-- Migration 079: Backfill tenant deterministic extraction/intent/preference/correction layer
-- Date: 2026-06-11
-- Purpose: Repair EXISTING per-tenant schemas that are missing the 8 tables backing the
--          deterministic (non-GLiNER2) extraction, intent-class enrichment, preference-signal,
--          and correction-signal logic.
--
-- WHY THIS IS NEEDED
-- ------------------
-- The ingest/query request connection runs with `SET search_path TO {schema}` WITHOUT public
-- (the public fallback was removed in commit 31580f6 to stop cross-schema write leakage).
-- The deterministic readers in src/api/main.py (_detect_atomic_values,
-- _build_intent_descriptions_for_gliner2, correction_signals load) and src/extraction/compound.py
-- read these tables UNQUALIFIED. The bootstrap/template previously seeded rel_types /
-- entity_taxonomies / negation_patterns / correction_patterns but FORGOT these 8 tables.
-- On a freshly-provisioned tenant the unqualified reads hit "relation does not exist"
-- (swallowed by bare except) and atomic scalars (ages), preferences, intent descriptions,
-- and correction signals are silently DROPPED.
--
-- The fix is to provision + seed these tables INTO each tenant so the existing unqualified
-- reads resolve in-tenant. public is the seed source ONLY and is never read at runtime;
-- we do NOT re-add public to search_path and do NOT qualify runtime reads to public.
--
-- SEED tables (copy data from public):
--   extraction_patterns (~53), intent_pattern_cache (~15), correction_signals (~12),
--   preference_patterns (8), intent_classes (4)
-- CREATE-only tables (empty/derived):
--   correction_signal_evaluations, extraction_pattern_matches, pattern_semantic_map
--
-- DDL mirrors the live public schema (origin migrations 032-037, 055-058, 066).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING.
-- Safe to run repeatedly. No DROP, no destructive SQL.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- ================================================================
        -- 1. CREATE the 8 tables in-tenant (idempotent)
        -- ================================================================

        -- correction_signals (SEED)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.correction_signals (
                id                   SERIAL PRIMARY KEY,
                pattern              TEXT NOT NULL UNIQUE,
                pattern_type         VARCHAR(50) NOT NULL,
                applicable_rel_types TEXT[] DEFAULT ARRAY[]::TEXT[],
                priority             INT DEFAULT 1,
                confidence           FLOAT DEFAULT 0.8,
                category             VARCHAR(50),
                example_usage        TEXT,
                created_at           TIMESTAMP DEFAULT now(),
                updated_at           TIMESTAMP DEFAULT now(),
                notes                TEXT,
                user_id              TEXT,
                success_count        INTEGER DEFAULT 0,
                last_applied_at      TIMESTAMPTZ,
                extraction_hints     JSONB,
                seed_confidence      FLOAT,
                semantics            TEXT,
                occurrence_count     INTEGER NOT NULL DEFAULT 1
            )$ddl$, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_signals_type ON %I.correction_signals(pattern_type)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_signals_priority ON %I.correction_signals(priority)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_signals_user_confidence ON %I.correction_signals (user_id, confidence DESC, created_at DESC) WHERE user_id IS NOT NULL', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_signals_hints ON %I.correction_signals (confidence DESC) WHERE extraction_hints IS NOT NULL', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_signals_semantics ON %I.correction_signals (user_id, semantics, confidence DESC)', _schema);

        -- correction_signal_evaluations (CREATE-only)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.correction_signal_evaluations (
                id                     SERIAL PRIMARY KEY,
                user_id                TEXT NOT NULL,
                candidate_pattern      TEXT NOT NULL,
                pattern_type           VARCHAR(50),
                first_text_snippet     TEXT,
                occurrence_count       INT DEFAULT 1,
                first_seen_at          TIMESTAMP DEFAULT now(),
                last_seen_at           TIMESTAMP DEFAULT now(),
                re_embedder_decision   VARCHAR(20),
                re_embedder_confidence FLOAT,
                approved_pattern_id    INT REFERENCES %I.correction_signals(id),
                created_at             TIMESTAMP DEFAULT now(),
                UNIQUE (user_id, candidate_pattern)
            )$ddl$, _schema, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_evals_decision ON %I.correction_signal_evaluations(re_embedder_decision)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_evals_pending ON %I.correction_signal_evaluations(re_embedder_decision, occurrence_count)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_correction_evals_last_seen ON %I.correction_signal_evaluations(last_seen_at)', _schema);

        -- pattern_semantic_map (CREATE-only)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.pattern_semantic_map (
                id                   SERIAL PRIMARY KEY,
                user_id              TEXT NOT NULL,
                pattern              TEXT NOT NULL,
                semantics            TEXT NOT NULL,
                confidence           FLOAT DEFAULT 0.5,
                applicable_rel_types TEXT[] DEFAULT ARRAY[]::TEXT[],
                confirmed_count      INT DEFAULT 0,
                created_at           TIMESTAMP DEFAULT now(),
                updated_at           TIMESTAMP DEFAULT now(),
                UNIQUE (user_id, pattern)
            )$ddl$, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_pattern_semantic_map_semantics ON %I.pattern_semantic_map(user_id, semantics)', _schema);

        -- intent_classes (SEED)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.intent_classes (
                id          SERIAL PRIMARY KEY,
                intent_name VARCHAR(50) NOT NULL UNIQUE,
                description TEXT NOT NULL,
                priority    INT DEFAULT 100,
                version     INT DEFAULT 1,
                refined_at  TIMESTAMP DEFAULT now(),
                is_active   BOOLEAN DEFAULT true,
                created_at  TIMESTAMP DEFAULT now(),
                updated_at  TIMESTAMP DEFAULT now(),
                refined_by  VARCHAR(255) DEFAULT 'bootstrap'
            )$ddl$, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_intent_classes_name ON %I.intent_classes (intent_name)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_intent_classes_active ON %I.intent_classes (is_active) WHERE is_active = true', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_intent_classes_priority ON %I.intent_classes (priority DESC) WHERE is_active = true', _schema);

        -- preference_patterns (SEED)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.preference_patterns (
                id              SERIAL PRIMARY KEY,
                pattern_text    VARCHAR(255) NOT NULL UNIQUE,
                signal_type     VARCHAR(50) NOT NULL,
                intent_name     VARCHAR(50) NOT NULL DEFAULT 'STATEMENT',
                base_confidence FLOAT NOT NULL DEFAULT 0.90,
                is_active       BOOLEAN DEFAULT true,
                created_at      TIMESTAMP DEFAULT now(),
                updated_at      TIMESTAMP DEFAULT now(),
                created_by      VARCHAR(255) DEFAULT 'bootstrap',
                CONSTRAINT chk_pp_base_confidence CHECK (base_confidence >= 0.0 AND base_confidence <= 1.0),
                CONSTRAINT chk_pp_signal_type CHECK (signal_type IN ('preference', 'alias', 'identity_correction')),
                CONSTRAINT chk_pp_intent_name CHECK (intent_name IN ('STATEMENT', 'QUERY', 'CORRECTION', 'RETRACTION'))
            )$ddl$, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_preference_patterns_active ON %I.preference_patterns(pattern_text) WHERE is_active = true', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_preference_patterns_by_type ON %I.preference_patterns(signal_type) WHERE is_active = true', _schema);

        -- extraction_patterns (SEED)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.extraction_patterns (
                id                SERIAL PRIMARY KEY,
                pattern_regex     VARCHAR(1024) NOT NULL,
                rel_type          VARCHAR(128) NOT NULL,
                frequency         INT DEFAULT 0,
                confirmed_count   INT DEFAULT 0,
                rejected_count    INT DEFAULT 0,
                correction_count  INT DEFAULT 0,
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
                UNIQUE (pattern_regex, rel_type)
            )$ddl$, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_extraction_patterns_active_confidence ON %I.extraction_patterns(is_active, global_confidence DESC)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_extraction_patterns_rel_type ON %I.extraction_patterns(rel_type)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_extraction_patterns_category ON %I.extraction_patterns(category)', _schema);

        -- extraction_pattern_matches (CREATE-only)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.extraction_pattern_matches (
                id           SERIAL PRIMARY KEY,
                pattern_id   INT REFERENCES %I.extraction_patterns(id) ON DELETE CASCADE,
                user_id      UUID,
                matched_text TEXT,
                matched_at   TIMESTAMP DEFAULT NOW(),
                confirmed    BOOLEAN,
                confirmed_at TIMESTAMP,
                created_at   TIMESTAMP DEFAULT NOW()
            )$ddl$, _schema, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_extraction_pattern_matches_pattern_id ON %I.extraction_pattern_matches(pattern_id)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_extraction_pattern_matches_confirmed ON %I.extraction_pattern_matches(confirmed)', _schema);

        -- intent_pattern_cache (SEED)
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.intent_pattern_cache (
                id                          SERIAL PRIMARY KEY,
                user_id                     VARCHAR(255) NOT NULL,
                pattern_text                TEXT NOT NULL,
                intent_type                 VARCHAR(50) NOT NULL,
                negation_type               VARCHAR(50),
                confidence                  FLOAT NOT NULL DEFAULT 0.60 CHECK (confidence >= 0.0 AND confidence <= 1.0),
                confirmed_count             INT NOT NULL DEFAULT 0,
                contradicted_count          INT NOT NULL DEFAULT 0,
                created_at                  TIMESTAMP DEFAULT now(),
                last_fired_at               TIMESTAMP,
                expires_at                  TIMESTAMP NOT NULL DEFAULT (now() + INTERVAL '3 days'),
                is_permanent                BOOLEAN NOT NULL DEFAULT false,
                min_context_chars           INT NOT NULL DEFAULT 0,
                requires_replacement_clause BOOLEAN NOT NULL DEFAULT false,
                learned_from                VARCHAR(100) DEFAULT 'bootstrap',
                UNIQUE (user_id, pattern_text, intent_type)
            )$ddl$, _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_intent_pattern_cache_lookup ON %I.intent_pattern_cache (user_id, confidence DESC)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_intent_pattern_cache_eviction ON %I.intent_pattern_cache (expires_at) WHERE is_permanent = false', _schema);

        -- ================================================================
        -- 2. SEED the 5 data-bearing tables from public (explicit columns,
        --    ON CONFLICT DO NOTHING). public is the seed source ONLY.
        -- ================================================================

        EXECUTE format($seed$
            INSERT INTO %I.extraction_patterns
                (pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 category, source, is_active, archived_at, last_matched_at)
            SELECT pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   category, source, is_active, archived_at, last_matched_at
            FROM public.extraction_patterns
            ON CONFLICT (pattern_regex, rel_type) DO NOTHING
        $seed$, _schema);

        EXECUTE format($seed$
            INSERT INTO %I.intent_classes
                (intent_name, description, priority, version, is_active, refined_by)
            SELECT intent_name, description, priority, version, is_active, refined_by
            FROM public.intent_classes
            ON CONFLICT (intent_name) DO NOTHING
        $seed$, _schema);

        EXECUTE format($seed$
            INSERT INTO %I.preference_patterns
                (pattern_text, signal_type, intent_name, base_confidence, is_active, created_by)
            SELECT pattern_text, signal_type, intent_name, base_confidence, is_active, created_by
            FROM public.preference_patterns
            ON CONFLICT (pattern_text) DO NOTHING
        $seed$, _schema);

        EXECUTE format($seed$
            INSERT INTO %I.correction_signals
                (pattern, pattern_type, applicable_rel_types, priority, confidence,
                 category, example_usage, notes, user_id, success_count, last_applied_at,
                 extraction_hints, seed_confidence, semantics, occurrence_count)
            SELECT pattern, pattern_type, applicable_rel_types, priority, confidence,
                   category, example_usage, notes, user_id, success_count, last_applied_at,
                   extraction_hints, seed_confidence, semantics, occurrence_count
            FROM public.correction_signals
            ON CONFLICT (pattern) DO NOTHING
        $seed$, _schema);

        EXECUTE format($seed$
            INSERT INTO %I.intent_pattern_cache
                (user_id, pattern_text, intent_type, negation_type, confidence,
                 confirmed_count, contradicted_count, last_fired_at, expires_at,
                 is_permanent, min_context_chars, requires_replacement_clause, learned_from)
            SELECT user_id, pattern_text, intent_type, negation_type, confidence,
                   confirmed_count, contradicted_count, last_fired_at, expires_at,
                   is_permanent, min_context_chars, requires_replacement_clause, learned_from
            FROM public.intent_pattern_cache
            ON CONFLICT (user_id, pattern_text, intent_type) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 079: seeded deterministic layer into %', _schema;
    END LOOP;
END $$;
