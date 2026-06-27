-- Migration 085: Backfill per-tenant entity_synonyms table + seed the synonym_of rel_type.
-- Date: 2026-06-12
--
-- PART A — entity_synonyms backfill (IMPL-1 data model)
-- ----------------------------------------------------
-- Referential-term → entity/relationship LINGUISTIC layer, kept strictly separate from
-- entity_aliases (the deterministic identity / proper-name layer). Synonyms are NEVER
-- is_preferred and never render as a display name (registry readers do not read this table).
-- DUPLICATES ALLOWED: only UNIQUE(term, entity_id), no cross-entity uniqueness — precision is
-- a read-time concern (SYNTHESIS #4). search_path has NO public at runtime, so the table must
-- exist INSIDE each tenant schema. NO public seed (synonyms are user-specific, created empty).
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS only. No DROP, no seed. Mirrors migration 079's loop.
--
-- PART B — synonym_of rel_type seed (GAP CLOSURE for IMPL-2 capture routing)
-- -------------------------------------------------------------------------
-- IMPL-2's capture router routes on rel_types.storage_target == 'entity_synonyms' (metadata
-- lookup, no hardcoded rel-name if). The 'entity_synonyms' storage_target is a NEW routing
-- target; the public.rel_types check_storage_target CHECK (migration 024) only allowed
-- ('facts','events','staged_only'), so it is ALTERed below to add 'entity_synonyms'. The
-- per-tenant rel_types table (user_schema.sql template) has NO such CHECK, so no per-tenant
-- ALTER is needed. The term tail is a SCALAR string (the literal synonym word), NOT a UUID.
-- New tenants inherit synonym_of automatically via the bootstrap
-- `INSERT INTO rel_types SELECT * FROM public.rel_types` (schema_manager.py:288). Existing
-- tenants are backfilled in the loop below, mirroring migration 083's two-step seed convention.
--
-- Idempotent throughout: CREATE ... IF NOT EXISTS; INSERT ... ON CONFLICT DO NOTHING; guarded
-- UPDATE (only fills the freshly-inserted skeleton row); the CHECK ALTER is guarded on conname.
-- Safe to re-run.

-- ============================================================================
-- PART A: Backfill entity_synonyms into every existing tenant schema
-- ============================================================================

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%' ESCAPE '\'
    LOOP
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.entity_synonyms (
                id               SERIAL PRIMARY KEY,
                entity_id        TEXT NOT NULL,
                term             TEXT NOT NULL,
                link_basis       TEXT NOT NULL DEFAULT 'entity',
                role_rel_type    TEXT,
                source           TEXT NOT NULL DEFAULT 'user',
                fact_provenance  TEXT NOT NULL DEFAULT 'user_stated',
                confidence       FLOAT NOT NULL DEFAULT 1.0,
                approved         BOOLEAN NOT NULL DEFAULT true,
                occurrence_count INT NOT NULL DEFAULT 1,
                sensitivity      VARCHAR(50) NOT NULL DEFAULT 'normal',
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                superseded_at    TIMESTAMPTZ,
                superseded_by    TEXT,
                UNIQUE (term, entity_id),
                FOREIGN KEY (entity_id) REFERENCES %I.entities(id) ON DELETE CASCADE,
                CONSTRAINT chk_es_link_basis  CHECK (link_basis IN ('entity','relationship')),
                CONSTRAINT chk_es_source      CHECK (source IN ('user','extract','llm_learned')),
                CONSTRAINT chk_es_provenance  CHECK (fact_provenance IN ('user_stated','llm_inferred','llm_learned')),
                CONSTRAINT chk_es_sensitivity CHECK (sensitivity IN ('normal','sensitive')),
                CONSTRAINT chk_es_role        CHECK (
                    (link_basis = 'entity'       AND role_rel_type IS NULL) OR
                    (link_basis = 'relationship' AND role_rel_type IS NOT NULL)
                )
            )$ddl$, _schema, _schema);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_entity_synonyms_term ON %I.entity_synonyms (term) WHERE superseded_at IS NULL',
            _schema);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_entity_synonyms_entity ON %I.entity_synonyms (entity_id)',
            _schema);

        RAISE NOTICE 'Migration 085: created entity_synonyms in %', _schema;
    END LOOP;
END $$;

-- ============================================================================
-- PART B1: Extend the public.rel_types storage_target CHECK to admit 'entity_synonyms'
-- ============================================================================
-- Migration 024 added check_storage_target CHECK (storage_target IN ('facts','events','staged_only'))
-- to public.rel_types ONLY. 'entity_synonyms' is a new routing target; drop+re-add the CHECK to
-- include it. Guarded: only acts if the constraint exists (idempotent on re-run — the re-added
-- constraint already admits the value, and DROP IF EXISTS tolerates absence).

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'check_storage_target'
    ) THEN
        ALTER TABLE public.rel_types DROP CONSTRAINT check_storage_target;
    END IF;
    ALTER TABLE public.rel_types
        ADD CONSTRAINT check_storage_target
        CHECK (storage_target IN ('facts', 'events', 'staged_only', 'entity_synonyms'));
END $$;

-- ============================================================================
-- PART B2: Seed synonym_of into public.rel_types (seed source / template)
-- ============================================================================
-- Two-step seed convention (cf. migrations 083, 005 + 030).
-- tail_types = {SCALAR} → the object is the literal synonym term STRING, NOT a UUID.

-- B2a. Insert the row (skeleton).
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, engine_generated, confidence, source, correction_behavior)
VALUES
    ('synonym_of', 'Synonym Of', NULL, false, 1.0, 'builtin', 'supersede')
ON CONFLICT (rel_type) DO NOTHING;

-- B2b. Populate metadata (guarded — only fills the freshly-inserted skeleton row; the
--      head_types guard makes this idempotent and prevents clobbering hand-tuned values).
UPDATE public.rel_types SET
    head_types          = ARRAY['ANY']::TEXT[],
    tail_types          = ARRAY['SCALAR']::TEXT[],
    is_symmetric        = false,
    inverse_rel_type    = NULL,
    is_hierarchy_rel    = false,
    fact_class          = 'B',
    storage_target      = 'entity_synonyms',
    category            = 'linguistic',
    natural_language    = 'X is also called Y'
WHERE rel_type = 'synonym_of'
  AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- PART B3: Seed synonym_of into every existing tenant rel_types
-- ============================================================================
-- New tenants inherit the row from the template's `INSERT INTO rel_types SELECT * FROM
-- public.rel_types` (schema_manager.py:288). Existing tenants need it pushed in. The overlay
-- (rel_type_overlay.py) then surfaces it at runtime — no code change required.
-- The per-tenant rel_types table has NO check_storage_target CHECK (only the public table does),
-- so 'entity_synonyms' inserts cleanly per-tenant.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%' ESCAPE '\'
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            -- B3a. Insert skeleton row (ON CONFLICT DO NOTHING — never clobber).
            EXECUTE format($ins$
                INSERT INTO %I.rel_types
                    (rel_type, label, wikidata_pid, engine_generated, confidence,
                     source, correction_behavior)
                VALUES
                    ('synonym_of', 'Synonym Of', NULL, false, 1.0,
                     'builtin', 'supersede')
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);

            -- B3b. Populate metadata (guarded on missing head_types — idempotent).
            EXECUTE format($upd$
                UPDATE %I.rel_types SET
                    head_types          = ARRAY['ANY']::TEXT[],
                    tail_types          = ARRAY['SCALAR']::TEXT[],
                    is_symmetric        = false,
                    inverse_rel_type    = NULL,
                    is_hierarchy_rel    = false,
                    fact_class          = 'B',
                    storage_target      = 'entity_synonyms',
                    category            = 'linguistic',
                    natural_language    = 'X is also called Y'
                WHERE rel_type = 'synonym_of'
                  AND (head_types IS NULL OR head_types = '{}')
            $upd$, _schema);

            RAISE NOTICE 'Migration 085: minted synonym_of into %', _schema;
        END IF;
    END LOOP;
END $$;
