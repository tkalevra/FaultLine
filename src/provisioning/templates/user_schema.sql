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

-- USER SCHEMA TEMPLATE: Replace {schema_name} with actual schema name (e.g., faultline_alice)
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
    fact_provenance TEXT NOT NULL DEFAULT 'llm_inferred',
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
    -- Temporal model (migration 088): the fact's TENSE + when it happened. Distinct
    -- from created_at (ingest time) and from a date that IS the object (a scalar in
    -- entity_attributes.value_date). Orthogonal to supersession.
    temporal_status TEXT NOT NULL DEFAULT 'now',
    event_date TIMESTAMPTZ,
    -- Granularity of event_date (migration 098): year|month|day|timestamp. NULL = unstamped.
    -- event_date is stamped at the START of the granule; range queries expand granularity
    -- -> [start, end) so "in 2025" doesn't break on a fabricated single instant.
    event_date_granularity TEXT,
    -- Assertion polarity (migration 114): the POLARITY of the user's assertion (ConText/NegEx).
    -- 'affirmed' (default) | 'negated'. A 'negated' fact is a definite NEGATED genuine state
    -- ("the GPS is not functioning") that must read back negated, never as its positive opposite.
    -- Orthogonal to temporal_status (tense) and to supersession/archival (belief currency).
    polarity TEXT NOT NULL DEFAULT 'affirmed',
    -- Tombstone (migration 097, Phase 4): FORGET marker, distinct from superseded_at/archived_at.
    -- NULL = live; non-NULL = forgotten (hidden from ALL reads, purge target). A forget sets
    -- archived_at + deleted_at together (recoverable for the grace window); un-forget clears both.
    deleted_at TIMESTAMPTZ,
    -- Citable provenance (migration 128): document/web source reference (URL, filename,
    -- title) threaded from the ingest_document lane. NULL for conversational facts.
    source_ref TEXT,
    UNIQUE(subject_id, object_id, rel_type),
    CONSTRAINT chk_facts_fact_provenance CHECK (fact_provenance IN ('user_stated', 'llm_inferred', 'llm_learned')),
    CONSTRAINT chk_facts_temporal_status CHECK (temporal_status IN ('now', 'past', 'future')),
    CONSTRAINT chk_facts_event_date_granularity CHECK (event_date_granularity IS NULL OR event_date_granularity IN ('year', 'month', 'day', 'timestamp')),
    CONSTRAINT chk_facts_polarity CHECK (polarity IN ('affirmed', 'negated'))
);

-- Idempotent backfill for tenant schemas created before the temporal model (migration 088).
ALTER TABLE facts ADD COLUMN IF NOT EXISTS temporal_status TEXT NOT NULL DEFAULT 'now';
ALTER TABLE facts ADD COLUMN IF NOT EXISTS event_date TIMESTAMPTZ;
-- Idempotent backfill for tenant schemas created before migration 098.
ALTER TABLE facts ADD COLUMN IF NOT EXISTS event_date_granularity TEXT;
-- Idempotent backfill for tenant schemas created before migration 114 (assertion polarity).
ALTER TABLE facts ADD COLUMN IF NOT EXISTS polarity TEXT NOT NULL DEFAULT 'affirmed';
-- Idempotent backfill for tenant schemas created before migration 097 (tombstone).
ALTER TABLE facts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
-- Idempotent backfill for tenant schemas created before migration 128 (citable provenance).
ALTER TABLE facts ADD COLUMN IF NOT EXISTS source_ref TEXT;

CREATE INDEX IF NOT EXISTS idx_facts_deleted_at
    ON facts (deleted_at) WHERE deleted_at IS NOT NULL;

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
CREATE INDEX IF NOT EXISTS idx_facts_event_date
    ON facts (event_date);
CREATE INDEX IF NOT EXISTS idx_facts_temporal
    ON facts (temporal_status, event_date);

-- Staged facts table: lower-confidence facts awaiting promotion
CREATE TABLE IF NOT EXISTS staged_facts (
    id SERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    rel_type TEXT,
    fact_class TEXT DEFAULT 'B',
    provenance TEXT,
    fact_provenance TEXT NOT NULL DEFAULT 'llm_inferred',
    confidence FLOAT NOT NULL DEFAULT 0.6,
    unified_confidence DOUBLE PRECISION DEFAULT 0.8,
    confirmed_count INT DEFAULT 0,
    hit_count INT NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- THE CLASS-C CLOCK (migration 012). Short-term memory is short-term BECAUSE it expires:
    -- a Class C row has exactly two legitimate destinies, it expires or it earns promotion to
    -- B. This template previously declared the column with NO default, silently dropping
    -- migration 012's `NOT NULL DEFAULT now() + interval '30 days'` for every tenant built
    -- from it. A row born with a NULL expiry is outside the lifecycle in BOTH directions —
    -- `expire_staged_facts` / `decay_class_c_hits` key on `expires_at <= now()` and NULL never
    -- satisfies it, while `fetch_unsynced_staged` and the recall lanes that filter
    -- `expires_at > now()` never see it either. Not NOT NULL, because live tenants carry NULL
    -- rows the backfill below repairs; the DEFAULT is what makes new rows correct.
    expires_at TIMESTAMPTZ DEFAULT (now() + interval '30 days'),
    promoted_at TIMESTAMPTZ,
    qdrant_synced BOOLEAN NOT NULL DEFAULT false,
    rel_type_definition TEXT DEFAULT '',
    storage_type TEXT,
    is_hierarchy_rel BOOLEAN DEFAULT false,
    taxonomies TEXT[] DEFAULT '{}',
    -- Temporal model (migration 088): Class C mirror of facts — identical structure.
    temporal_status TEXT NOT NULL DEFAULT 'now',
    event_date TIMESTAMPTZ,
    -- Granularity of event_date (migration 098): Class C mirror of facts.
    event_date_granularity TEXT,
    -- Assertion polarity (migration 114): Class C mirror of facts.polarity — 'affirmed' | 'negated'.
    polarity TEXT NOT NULL DEFAULT 'affirmed',
    -- Tombstone (migration 097, Phase 4): FORGET marker. NULL = live; non-NULL = forgotten.
    -- A tombstoned staged row is frozen from lifecycle bumps (no expires_at reset / no
    -- confirmed_count / hit_count bump) so it cannot re-promote or un-expire; un-forget clears it.
    deleted_at TIMESTAMPTZ,
    -- Citable provenance (migration 128): mirror of facts.source_ref — document/web source
    -- reference threaded from the ingest_document lane; survives B→facts promotion.
    source_ref TEXT,
    UNIQUE(subject_id, object_id, rel_type),
    CONSTRAINT chk_staged_facts_fact_provenance CHECK (fact_provenance IN ('user_stated', 'llm_inferred', 'llm_learned')),
    CONSTRAINT chk_staged_facts_temporal_status CHECK (temporal_status IN ('now', 'past', 'future')),
    CONSTRAINT chk_staged_facts_event_date_granularity CHECK (event_date_granularity IS NULL OR event_date_granularity IN ('year', 'month', 'day', 'timestamp')),
    CONSTRAINT chk_staged_facts_polarity CHECK (polarity IN ('affirmed', 'negated'))
);

-- Idempotent backfill for tenant schemas created before the temporal model (migration 088).
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS temporal_status TEXT NOT NULL DEFAULT 'now';
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS event_date TIMESTAMPTZ;
-- Idempotent backfill for tenant schemas created before migration 098.
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS event_date_granularity TEXT;
-- Idempotent backfill for tenant schemas created before migration 114 (assertion polarity).
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS polarity TEXT NOT NULL DEFAULT 'affirmed';
-- Idempotent backfill for tenant schemas created before migration 097 (tombstone).
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
-- Idempotent backfill for tenant schemas created before migration 128 (citable provenance).
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS source_ref TEXT;
-- Belt-and-braces for the Class-C clock. `ADD COLUMN IF NOT EXISTS` cannot repair a column that
-- already exists with the wrong default, so state it explicitly here too; naturally idempotent.
-- NOTE: this template is applied ONLY at schema creation, so it cannot reach tenants that were
-- already built without the default — migration 264 is what repairs those, and it must stay
-- paired with the CREATE TABLE default above.
ALTER TABLE staged_facts ALTER COLUMN expires_at SET DEFAULT (now() + interval '30 days');

CREATE INDEX IF NOT EXISTS idx_staged_facts_deleted_at
    ON staged_facts (deleted_at) WHERE deleted_at IS NOT NULL;

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
CREATE INDEX IF NOT EXISTS idx_staged_facts_event_date
    ON staged_facts (event_date);
CREATE INDEX IF NOT EXISTS idx_staged_facts_temporal
    ON staged_facts (temporal_status, event_date);

-- Entities: canonical entity registry (per-user schema, no user_id needed — schema isolation is sufficient)
CREATE TABLE IF NOT EXISTS entities (
    id TEXT NOT NULL PRIMARY KEY,
    entity_type TEXT DEFAULT 'unknown',
    -- FIRST-CLASS value/place property (THE HARD LINE, migration 192). {value,place,name,both};
    -- NULL = unstamped/legacy (falls through to structural re-derivation). value/name/both = a
    -- MEMORY (never laddered/typed/superseded by inferred type); place = an L4 type node; both =
    -- OWL punning. Derived/stamped/read by src/api/node_role.py under VALUE_PLACE_FIRST_CLASS
    -- (default OFF). See the internal design record
    node_role TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entities_type
    ON entities (entity_type);

-- Entity aliases: preferred names and alternate names for entities
CREATE TABLE IF NOT EXISTS entity_aliases (
    id SERIAL PRIMARY KEY,
    entity_id TEXT NOT NULL,
    -- COLLATE "es-x-icu" (migration 216; [es branch] Spanish ICU tailoring, ñ after n — the
    -- English line uses "en-US-x-icu"): this database is created by an image whose libc
    -- (musl, on postgres:*-alpine) cannot resolve the `en_US.utf8` it DECLARES, so the default
    -- collation silently degrades to BYTE ordering — `école` and `résumé` sort after `zebra`.
    -- `alias` is the display sort key (13 `ORDER BY ... alias` sites, several `LIMIT 1`, so the
    -- collation picks the rendered name), hence the column-level collation: every unqualified
    -- `ORDER BY alias` resolves against it, with no query carrying a COLLATE clause.
    -- DETERMINISTIC by design — it changes ORDERING ONLY. Equality stays bytewise, so `alias`
    -- keeps every other semantic intact: the matching key, the dedup key, the UUID-v5 input,
    -- `UNIQUE (entity_id, alias)` and the `ON CONFLICT` target. A NON-deterministic collation
    -- would make 'ete' = 'été' and collapse distinct people onto one row — never use one here.
    alias TEXT NOT NULL COLLATE "es-x-icu",
    -- OBSERVED CASING OVERLAY of `alias` (migration 214). NULL = no casing was observed for
    -- this name, and the renderer falls back to `alias` — i.e. today's lowercase output.
    -- INVARIANT, enforced in Python at the write seam (src/extraction/display_case.py), NOT
    -- as a SQL CHECK: lower(display_form) = alias. It is a CASING overlay and can never be a
    -- second, divergent name. `alias` stays the matching/dedup/UUID-v5/ON CONFLICT key and is
    -- read by everything except the ONE sanctioned read-time presentation seam in /query.
    -- (No CHECK because PostgreSQL's collation-dependent lower() can disagree with Python's
    -- str.lower() on non-ASCII, and a violation there would cost the user's whole sentence.)
    -- COLLATE as above (migration 216) — this is the column that is actually RENDERED, so it
    -- must not be the one left on byte ordering. It carries no index, so the collation is free.
    display_form TEXT DEFAULT NULL COLLATE "es-x-icu",
    is_preferred BOOLEAN NOT NULL DEFAULT false,
    preference_source TEXT NOT NULL DEFAULT 'unspecified',
    -- The AUTHORITY that licensed this surface to co-refer with the entity's other labels
    -- (e.g. 'wordnet_synset', 'abbreviation_definition'), or NULL for no recorded licence.
    -- ORTHOGONAL to preference_source: that column records TRUST and is arbitrated by rank,
    -- this one records JUSTIFICATION and must be merged by UNION, never by rank.
    -- Added here 2026-08-27 to close a MEASURED template gap: migration 244 fans this column
    -- into existing tenants, but the template never gained it, so every freshly minted tenant
    -- was born one column short and depended on the next boot's fan-out to repair it. That gap
    -- was the ONLY structural difference between a fresh mint and a fully-swept one, and closing
    -- it is what makes stamping a new tenant migration-current provably safe
    -- (src/provisioning/boot_migrations.py::stamp_schema_as_current). Pinned by
    -- tests/test_boot_migrations.py::test_fresh_mint_is_migration_equivalent.
    coreference_warrant TEXT,
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
-- Partial index (migration 244): warranted rows are the rare case and are the ones the
-- alias co-reference guard asks about.
CREATE INDEX IF NOT EXISTS idx_entity_aliases_coreference_warrant
    ON entity_aliases (entity_id) WHERE coreference_warrant IS NOT NULL;

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
    -- SCALAR-TYPE discipline (migration 101): the detected/derived datatype of this leaf
    -- memory (closed XSD/Wikidata-literal set), the canonical unit (for quantity), and a
    -- normalized form (e.g. lowercased FQDN). NULL = legacy / untyped. Scalars stay leaf
    -- memories here — typed for validation + retrieval, NEVER L4-placed / vector-indexed.
    datatype TEXT NULL,
    unit TEXT NULL,
    value_normalized TEXT NULL,
    PRIMARY KEY (entity_id, attribute),
    UNIQUE (entity_id, attribute),
    FOREIGN KEY (entity_id) REFERENCES entities(id),
    CONSTRAINT chk_entity_attributes_datatype CHECK (datatype IS NULL OR datatype IN (
        'integer','decimal','quantity','date','datetime','string','ipv4','ipv6',
        'cidr','mac','email','url','fqdn','phone','uuid','boolean','currency',
        'percentage','coordinate','duration'))
);

CREATE INDEX IF NOT EXISTS idx_entity_attributes_user_id
    ON entity_attributes (user_id);
CREATE INDEX IF NOT EXISTS idx_entity_attributes_user_entity
    ON entity_attributes (user_id, entity_id);

-- SCALAR SUPERSESSION HISTORY (scalar-supersession gauntlet, 2026-09-03). The attribute
-- lane's PK is single-row (entity_id, attribute), so an in-place value change DESTROYS the
-- prior value with no retirement stamp and no audit trail -- the one user-value store
-- without the bi-temporal discipline the facts lane already has (facts supersede by
-- stamping superseded_at and retaining the row; entity_attributes cannot retain because
-- the next value OVERWRITES the only row). This APPEND-ONLY ledger is the SS1 alternative
-- sanctioned by the spec: every supersede/retire writes one row carrying the PRIOR value
-- snapshot, the NEW value snapshot, timestamp, provenance of both sides, and the cause.
-- THE HARD LINE: history rows are CONTENT, never places -- no entity minting, no L4, no
-- rel_type rows are ever grown from here. Values are user truth; this table is its
-- audit surface. Tenant-schema-resident by construction (never public -- SS5 isolation).
-- Existing tenant schemas receive it via migrations/273 (boot-migration ledger); new
-- tenants get it here at provisioning.
CREATE TABLE IF NOT EXISTS entity_attributes_history (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    entity_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    -- PRIOR (superseded/retired) value snapshot -- the recoverable history (SS1)
    prior_value_text TEXT,
    prior_value_int INT,
    prior_value_float DOUBLE PRECISION,
    prior_value_date DATE,
    prior_provenance TEXT,
    prior_datatype TEXT,
    prior_created_at TIMESTAMP WITH TIME ZONE,
    -- SUPERSEDING (new) value snapshot -- NULL on a pure retire (retraction/negation)
    new_value_text TEXT,
    new_value_int INT,
    new_value_float DOUBLE PRECISION,
    new_value_date DATE,
    new_provenance TEXT,
    -- 'superseded' = value replaced (correction/re-statement); 'retired' = value removed
    -- (retraction/negation, row stamped superseded_at); 'restated' = a retired slot's value
    -- re-asserted by a genuine fresh user statement (un-retired).
    action TEXT NOT NULL CHECK (action IN ('superseded', 'retired', 'restated')),
    -- what wrote this row: 'ingest_restate' | 'correct_fact' | 'retract_lane' |
    -- 'negation_retire' (free-form diagnostic vocabulary, not a closed enum)
    cause TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_entity_attributes_history_slot
    ON entity_attributes_history (entity_id, attribute);
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
    anomaly_threshold FLOAT,
    mutually_exclusive_with TEXT[] DEFAULT '{}',
    -- Second-person recall template: used at render time ONLY when the SUBJECT
    -- slot resolves to "you" (the querying user). Keeps the object slot Y; the
    -- subject is baked in ("You are the parent of Y"). NULL → 3p fallback + fixup.
    -- Appended LAST to match migration 081's ALTER ... ADD COLUMN ordinal so the
    -- bootstrap `INSERT INTO rel_types SELECT * FROM public.rel_types` stays aligned.
    natural_language_2p TEXT,
    -- Temporal class (migration 096, DESIGN-memory-temporal-lifecycle §3.1): drives
    -- supersede-vs-coexist deterministically. SAFE DEFAULT 'state' (non-destructive) so a
    -- newly-grown rel never wrongly hard-supersedes. Appended LAST to match migration 096's
    -- ALTER ... ADD COLUMN ordinal in public so the bootstrap
    -- `INSERT INTO rel_types SELECT * FROM public.rel_types` stays aligned.
    temporal_class TEXT NOT NULL DEFAULT 'state',
    -- SCALAR-TYPE discipline (migration 101): the DATATYPE of a SCALAR slot is a first-class
    -- metadata property (validation + retrieval). NULL for non-SCALAR rels. Closed set
    -- modeled on XSD / Wikidata literal datatypes. value_min/value_max/unit carry range +
    -- canonical unit for quantity/integer datatypes. Appended LAST to match migration 101's
    -- ALTER ... ADD COLUMN ordinal in public so the bootstrap
    -- `INSERT INTO rel_types SELECT * FROM public.rel_types` stays aligned.
    scalar_datatype TEXT NULL,
    value_min DOUBLE PRECISION NULL,
    value_max DOUBLE PRECISION NULL,
    unit TEXT NULL,
    -- CARDINALITY (migration 168, DESIGN-memory-temporal-lifecycle §5): does the rel hold at
    -- most ONE CURRENT value per subject? TRUE = OWL-functional STATE (residence) → recency
    -- picks the latest as current, older coexisting undated 'now' value(s) render as FORMER.
    -- FALSE (default, non-destructive) = multi-valued (speaks/likes) → all values stay current.
    -- Read by the query render seams; ingest/supersession unchanged (both values still coexist).
    -- Appended LAST to match migration 168's ALTER … ADD COLUMN ordinal in public so the
    -- bootstrap `INSERT INTO rel_types SELECT * FROM public.rel_types` stays aligned.
    is_single_valued BOOLEAN NOT NULL DEFAULT false,
    CONSTRAINT rel_types_source_check CHECK (source = ANY (ARRAY['wikidata', 'builtin', 'engine', 'user', 'expand'])),
    CONSTRAINT chk_rel_types_temporal_class CHECK (temporal_class IN ('immutable', 'state', 'event')),
    CONSTRAINT chk_rel_types_scalar_datatype CHECK (scalar_datatype IS NULL OR scalar_datatype IN (
        'integer','decimal','quantity','date','datetime','string','ipv4','ipv6',
        'cidr','mac','email','url','fqdn','phone','uuid','boolean','currency',
        'percentage','coordinate','duration'))
);

CREATE INDEX IF NOT EXISTS idx_rel_types_engine_generated
    ON rel_types (engine_generated);
CREATE INDEX IF NOT EXISTS idx_rel_types_category
    ON rel_types (category);
CREATE INDEX IF NOT EXISTS idx_rel_types_is_hierarchy
    ON rel_types (is_hierarchy_rel);

-- Rel-type aliases (per-tenant copy of the public template, migrations 030/031).
-- Maps non-canonical rel_type variations AND relationship ROLE-NOUNS (mother→parent_of,
-- boss→works_for, …) to canonical rel_types. Read UNQUALIFIED at runtime on the tenant
-- search_path (no public) by possessive-relationship anchor resolution. Seeded from
-- public.rel_type_aliases at provisioning (schema_manager._seed_rel_type_aliases) — public
-- is the SEED SOURCE only, never read at runtime. Column set mirrors migrations 030 + 031.
CREATE TABLE IF NOT EXISTS rel_type_aliases (
    id SERIAL PRIMARY KEY,
    canonical_rel_type VARCHAR(255) NOT NULL,
    alias VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT NOW(),
    source VARCHAR(50) DEFAULT 'ontology',
    confidence FLOAT DEFAULT 1.0,
    requires_inversion BOOLEAN DEFAULT FALSE,
    is_symmetric BOOLEAN DEFAULT FALSE,
    inverse_alias VARCHAR(255),
    FOREIGN KEY (canonical_rel_type) REFERENCES rel_types(rel_type) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_alias ON rel_type_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_rel_type_aliases_canonical ON rel_type_aliases(canonical_rel_type);

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
    -- Nesting (rung 4, migration 087): a hierarchical group contains entities
    -- (member_entity_types) ∪ references to sub-groups (member_taxonomies).
    -- e.g. family ⊃ {pets}, pets ⊃ {animal}. Empty for flat groups.
    member_taxonomies TEXT[] DEFAULT '{}',
    -- Structural correction (DESIGN-hierarchy-ladder §"user correction is REAL"):
    -- sub-groups the USER has explicitly SEVERED from this group ("my pets are not
    -- part of my family" → 'pets' lands here, removed from member_taxonomies).
    -- This is the DURABLE, NOT-superseded lock: the background nesting-growth engine
    -- MUST consult this and refuse to re-add a user-severed link. User > engine.
    severed_taxonomies TEXT[] DEFAULT '{}',
    -- Provenance (Phase 2/2C alignment with migration 019): distinguishes a
    -- user-corrected/curated scope row from a seeded one. Phase 3 scope
    -- corrections set this to 'user_corrected'; the seeder copies public's value.
    source TEXT DEFAULT 'seeded',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotent backfill for tenant schemas created before `source` was added to
-- this template (safe to run repeatedly; templates are applied per-provision).
ALTER TABLE entity_taxonomies ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'seeded';

-- Idempotent backfill for the nesting column (migration 087); safe to re-run.
ALTER TABLE entity_taxonomies ADD COLUMN IF NOT EXISTS member_taxonomies TEXT[] DEFAULT '{}';

-- Idempotent backfill for the structural-correction sever lock; safe to re-run.
ALTER TABLE entity_taxonomies ADD COLUMN IF NOT EXISTS severed_taxonomies TEXT[] DEFAULT '{}';

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

-- Climb-state cache (migration 102): the re_embedder classification-climb VERDICT cache.
-- DB = cache. Keyed by the concept ENTITY being classified (a TYPE node UUID, never a name).
-- The ±6 climb + miss-pushback what-is classifier read this BEFORE any LLM call and skip
-- concepts already 'placed' / 'unplaceable' (unless the input fingerprint changed = additive
-- new info, or an 'unplaceable' is past its backoff window and under the attempt cap). Kills
-- the every-cycle re-sweep runaway. Per-tenant only; public is the seed source / template.
CREATE TABLE IF NOT EXISTS climb_state (
    entity_id       TEXT PRIMARY KEY,
    verdict         TEXT NOT NULL,            -- 'placed' | 'unplaceable'
    reason          TEXT,                     -- placed|cycle_rejected|no_parent|cap_hit|no_info_root
    attempt_count   INT  NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMP WITH TIME ZONE,
    fingerprint     TEXT,                     -- input fingerprint judged against; differs => re-open
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_climb_state_verdict
    ON climb_state (verdict, last_attempt_at);

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

-- Confidence gates: per-user adaptive intent-classification threshold (PER-SCHEMA).
-- Written by the re_embedder's bin-reliability self-tuning loop from this tenant's own
-- intent_confidence_feedback; read by /classify-intent and /confidence-gate. Starts empty
-- (no row → GATE_DEFAULT fallback). public.confidence_gates is template-only — never read or
-- written at runtime. Mirrors migration 043 (per-tenant, no public).
CREATE TABLE IF NOT EXISTS confidence_gates (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL UNIQUE,
    threshold FLOAT DEFAULT 0.70,
    adjusted_at TIMESTAMP DEFAULT now(),
    created_at TIMESTAMP DEFAULT now()
);

-- Correction patterns: regex patterns for pre-GLiNER2 correction intent detection (per-schema)
-- Checked BEFORE GLiNER2 runs to catch negation-correction phrases that GLiNER2 misclassifies
-- as QUERY (e.g. "X is a computer, not an animal"). Patterns are subject-agnostic structure
-- detectors — they match sentence shape, not specific entity names.
CREATE TABLE IF NOT EXISTS correction_patterns (
    id SERIAL PRIMARY KEY,
    pattern_text TEXT NOT NULL,
    confidence FLOAT DEFAULT 0.9,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE(pattern_text)
);

CREATE INDEX IF NOT EXISTS idx_correction_patterns_active
    ON correction_patterns (active) WHERE active = TRUE;

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

-- ----------------------------------------------------------------------------
-- DEFINING-GROUP ELIGIBILITY INVARIANT (migration 119)
-- ----------------------------------------------------------------------------
-- A rel may be a DEFINING rel of a taxonomy (rel_types_defining_group) only if it can
-- describe a HOMOGENEOUS membership group. This single trigger is the producer-agnostic
-- enforcement point: it catches every write (seeding, growth/re_embedder, ingest in-flow,
-- /learn, and any future producer) instead of guarding each call site. Subject-agnostic —
-- it reasons purely over types, never rel/place literals.
--   INVARIANT 1: a TYPE-CLASSIFICATION rel (is_hierarchy_rel AND wikidata_pid ∈ P31/P279 —
--     instance_of/subclass_of/is_a, "what IS this entity") is never a defining rel: it would
--     intercept nearly every typed-entity query. MEMBERSHIP/COMPOSITION hierarchy rels
--     (member_of/part_of/located_in — "belongs to a GROUP/whole/place", no classification PID)
--     ARE allowed to define a grouping (subject to INVARIANT 2): they only match edges into a
--     specific named group, not every typed entity. This mirrors the P31/P279 split used by
--     _get_classification_rels and is what lets an engine-grown collective ("my band") become a
--     walkable grouping node defined by its observed member_of edges. (migration 122)
--   INVARIANT 2 (cross-type guard): every CONCRETE head/tail type (excluding the ANY/SCALAR
--     sentinels) must be a member of this taxonomy. A cross-type asymmetric rel
--     (e.g. person->location for `lives_in`) cannot define a single-type group — it was the
--     root of the false hierarchy_membership_violation that demoted residence facts to Class C.
-- Fail-safe: a rel absent from rel_types (novel, not yet minted) or a taxonomy with no/`ANY`
-- members is KEPT (we never silently lose a defining rel we cannot judge). Each removal RAISEs
-- a loud WARNING. This is the producer-side twin of the read-time guard in wgm/gate.py.
CREATE OR REPLACE FUNCTION enforce_defining_group_eligibility()
RETURNS TRIGGER AS $$
DECLARE
    _rel     TEXT;
    _kept    TEXT[] := '{}';
    _is_hier BOOLEAN;
    _pid     TEXT;
    _heads   TEXT[];
    _tails   TEXT[];
    _members TEXT[];
BEGIN
    IF NEW.rel_types_defining_group IS NULL
       OR array_length(NEW.rel_types_defining_group, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    -- No concrete membership to judge against → keep as-is (fail-safe).
    IF NEW.member_entity_types IS NULL
       OR array_length(NEW.member_entity_types, 1) IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT array_agg(LOWER(m)) INTO _members FROM unnest(NEW.member_entity_types) m;

    FOREACH _rel IN ARRAY NEW.rel_types_defining_group LOOP
        SELECT is_hierarchy_rel, wikidata_pid, head_types, tail_types
          INTO _is_hier, _pid, _heads, _tails
          FROM rel_types WHERE rel_type = LOWER(_rel);

        IF NOT FOUND THEN
            _kept := array_append(_kept, _rel);          -- novel/unknown rel → keep
            CONTINUE;
        END IF;

        -- INVARIANT 1: drop only TYPE-CLASSIFICATION hierarchy rels (P31/P279 — instance_of/
        -- subclass_of/is_a). Membership/composition hierarchy rels (member_of/part_of/located_in)
        -- are kept here and arbitrated by the cross-type guard below.
        IF COALESCE(_is_hier, FALSE) AND COALESCE(_pid, '') IN ('P31', 'P279') THEN
            RAISE WARNING 'faultline: dropped classification rel % from defining group of taxonomy %',
                          _rel, NEW.taxonomy_name;
            CONTINUE;
        END IF;

        -- 'ANY' in the member set = wildcard membership → no cross-type constraint.
        IF NOT ('any' = ANY(_members)) THEN
            IF EXISTS (SELECT 1 FROM unnest(COALESCE(_heads, '{}')) h
                       WHERE LOWER(h) NOT IN ('any','scalar') AND LOWER(h) <> ALL(_members))
               OR EXISTS (SELECT 1 FROM unnest(COALESCE(_tails, '{}')) t
                          WHERE LOWER(t) NOT IN ('any','scalar') AND LOWER(t) <> ALL(_members))
            THEN
                RAISE WARNING 'faultline: dropped cross-type rel % from defining group of taxonomy % (head/tail not all members)',
                              _rel, NEW.taxonomy_name;
                CONTINUE;
            END IF;
        END IF;

        _kept := array_append(_kept, _rel);
    END LOOP;

    NEW.rel_types_defining_group := _kept;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_defining_group_eligibility_ins ON entity_taxonomies;
CREATE TRIGGER enforce_defining_group_eligibility_ins
    BEFORE INSERT ON entity_taxonomies
    FOR EACH ROW EXECUTE FUNCTION enforce_defining_group_eligibility();

DROP TRIGGER IF EXISTS enforce_defining_group_eligibility_upd ON entity_taxonomies;
CREATE TRIGGER enforce_defining_group_eligibility_upd
    BEFORE UPDATE ON entity_taxonomies
    FOR EACH ROW EXECUTE FUNCTION enforce_defining_group_eligibility();

-- system_alerts: persistent per-user warnings with countdown suppression
-- alert_type is unique per schema; alerts_shown tracks how many times the warning
-- has been surfaced to the user; backend suppresses further noise once
-- alerts_shown >= max_alerts; resolved_at is set when the condition clears.
-- Migration 062 backfills this table into existing provisioned schemas.
CREATE TABLE IF NOT EXISTS {schema_name}.system_alerts (
    id            SERIAL      PRIMARY KEY,
    alert_type    TEXT        NOT NULL,
    alert_count   INTEGER     NOT NULL DEFAULT 1,
    alerts_shown  INTEGER     NOT NULL DEFAULT 0,
    max_alerts    INTEGER     NOT NULL DEFAULT 4,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ,
    UNIQUE (alert_type)
);

-- ============================================================================
-- DETERMINISTIC EXTRACTION / INTENT / PREFERENCE / CORRECTION LAYER (per-tenant)
-- ----------------------------------------------------------------------------
-- These 8 tables back the deterministic (non-GLiNER2) extraction, intent-class
-- enrichment, preference-signal, and correction-signal logic. The ingest/query
-- request connection runs with `SET search_path TO {schema_name}` (NO public —
-- removed in 31580f6 to stop cross-schema write leakage), and the readers in
-- src/api/main.py (_detect_atomic_values, _build_intent_descriptions_for_gliner2,
-- correction_signals load) and src/extraction/compound.py read these tables
-- UNQUALIFIED. They therefore MUST exist INSIDE each tenant schema or the reads
-- silently hit "relation does not exist" (swallowed by bare except) and atomic
-- scalars (ages), preferences, intent descriptions, and correction signals are
-- dropped. DDL mirrors the public origin (migrations 032-037, 055-058, 066) as it
-- exists in the live public schema. Data for the 5 seed-bearing tables is copied
-- from public at provisioning time by schema_manager._execute_bootstrap_queries();
-- the 3 derived/empty tables are CREATE-only. public is the seed source ONLY and
-- is never read at runtime.
-- ============================================================================

-- correction_signals: implicit correction-signal patterns (per-tenant).
-- Read UNQUALIFIED in src/api/main.py (_build_intent_descriptions_for_gliner2).
-- Columns mirror public (migrations 032/034/035/036/067).
CREATE TABLE IF NOT EXISTS {schema_name}.correction_signals (
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
);
CREATE INDEX IF NOT EXISTS idx_correction_signals_type ON {schema_name}.correction_signals(pattern_type);
CREATE INDEX IF NOT EXISTS idx_correction_signals_priority ON {schema_name}.correction_signals(priority);
CREATE INDEX IF NOT EXISTS idx_correction_signals_user_confidence
    ON {schema_name}.correction_signals (user_id, confidence DESC, created_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_correction_signals_hints
    ON {schema_name}.correction_signals (confidence DESC) WHERE extraction_hints IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_correction_signals_semantics
    ON {schema_name}.correction_signals (user_id, semantics, confidence DESC);

-- correction_signal_evaluations: growth-tracking of candidate correction signals.
-- Read UNQUALIFIED in src/re_embedder/embedder.py. CREATE-only (derived). (migration 033)
CREATE TABLE IF NOT EXISTS {schema_name}.correction_signal_evaluations (
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
    approved_pattern_id    INT REFERENCES {schema_name}.correction_signals(id),
    created_at             TIMESTAMP DEFAULT now(),
    UNIQUE (user_id, candidate_pattern)
);
CREATE INDEX IF NOT EXISTS idx_correction_evals_decision ON {schema_name}.correction_signal_evaluations(re_embedder_decision);
CREATE INDEX IF NOT EXISTS idx_correction_evals_pending ON {schema_name}.correction_signal_evaluations(re_embedder_decision, occurrence_count);
CREATE INDEX IF NOT EXISTS idx_correction_evals_last_seen ON {schema_name}.correction_signal_evaluations(last_seen_at);

-- pattern_semantic_map: learned semantic mappings for correction patterns.
-- CREATE-only (derived). (migration 036)
CREATE TABLE IF NOT EXISTS {schema_name}.pattern_semantic_map (
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
);
CREATE INDEX IF NOT EXISTS idx_pattern_semantic_map_semantics ON {schema_name}.pattern_semantic_map(user_id, semantics);

-- intent_classes: semantic descriptions used to build GLiNER2 zero-shot intent labels.
-- Read UNQUALIFIED in src/api/main.py (_build_intent_descriptions_for_gliner2). (migrations 055/056)
CREATE TABLE IF NOT EXISTS {schema_name}.intent_classes (
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
);
CREATE INDEX IF NOT EXISTS idx_intent_classes_name ON {schema_name}.intent_classes (intent_name);
CREATE INDEX IF NOT EXISTS idx_intent_classes_active ON {schema_name}.intent_classes (is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_intent_classes_priority ON {schema_name}.intent_classes (priority DESC) WHERE is_active = true;

-- preference_patterns: subject-agnostic preference signal patterns (Layer 2c). (migration 057)
CREATE TABLE IF NOT EXISTS {schema_name}.preference_patterns (
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
);
CREATE INDEX IF NOT EXISTS idx_preference_patterns_active ON {schema_name}.preference_patterns(pattern_text) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_preference_patterns_by_type ON {schema_name}.preference_patterns(signal_type) WHERE is_active = true;

-- extraction_patterns: metadata-driven regex extraction patterns (deterministic layer).
-- Read UNQUALIFIED in src/extraction/compound.py, src/api/main.py (_detect_atomic_values,
-- scalar_atomic load), and src/re_embedder/embedder.py. (migration 058)
CREATE TABLE IF NOT EXISTS {schema_name}.extraction_patterns (
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
);
CREATE INDEX IF NOT EXISTS idx_extraction_patterns_active_confidence ON {schema_name}.extraction_patterns(is_active, global_confidence DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_patterns_rel_type ON {schema_name}.extraction_patterns(rel_type);
CREATE INDEX IF NOT EXISTS idx_extraction_patterns_category ON {schema_name}.extraction_patterns(category);

-- extraction_pattern_matches: weak-supervision feedback for extraction patterns.
-- CREATE-only (derived). (migration 058)
CREATE TABLE IF NOT EXISTS {schema_name}.extraction_pattern_matches (
    id           SERIAL PRIMARY KEY,
    pattern_id   INT REFERENCES {schema_name}.extraction_patterns(id) ON DELETE CASCADE,
    user_id      UUID,
    matched_text TEXT,
    matched_at   TIMESTAMP DEFAULT NOW(),
    confirmed    BOOLEAN,
    confirmed_at TIMESTAMP,
    created_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_extraction_pattern_matches_pattern_id ON {schema_name}.extraction_pattern_matches(pattern_id);
CREATE INDEX IF NOT EXISTS idx_extraction_pattern_matches_confirmed ON {schema_name}.extraction_pattern_matches(confirmed);

-- temporal_patterns: metadata-driven, GROWABLE relative-date CUE inventory (deterministic layer).
-- Read via the per-tenant overlay (src/api/temporal_pattern_overlay.py) by linguistics
-- `_classify_span_anchor` to split a date SPAN relative-vs-absolute for dateparser anchoring.
-- The closed FORMAL absolute checks (month names / numeric / 4-digit year) + _anchor_absolute_year
-- stay in code; ONLY the relative-cue recognition is DB data that grows (freq-gated, tenant-only).
-- Mirrors extraction_patterns lifecycle/confidence. Seeded from public.temporal_patterns at
-- provisioning (schema_manager.py). (migration 103)
CREATE TABLE IF NOT EXISTS {schema_name}.temporal_patterns (
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
    CONSTRAINT chk_temporal_anchor_type
        CHECK (anchor_type IN ('relative', 'absolute_no_year', 'explicit_year')),
    UNIQUE (pattern_regex, anchor_type)
);
CREATE INDEX IF NOT EXISTS idx_temporal_patterns_active ON {schema_name}.temporal_patterns(is_active, anchor_type);
CREATE INDEX IF NOT EXISTS idx_temporal_patterns_category ON {schema_name}.temporal_patterns(category);

-- linguistic_cues: metadata-driven, GROWABLE linguistic verb/particle CUE inventory (deterministic
-- layer). Read via the per-tenant overlay (src/api/linguistic_cue_overlay.py). General by `category`:
--   * 'naming_verb'      — predicative naming/dubbing verbs (analyze_naming / _event_title /
--                          is_naming_predicate): "a dog named/titled/dubbed X". (migration 105)
--   * 'lvc_support_verb' — light/support verbs governing an eventive object (analyze_event /
--                          analyze_svo_relations): have/go/attend/take/do/make/get/participate
--                          /volunteer. (mig 108; volunteer mig 191)
--   * 'svo_particle'     — load-bearing particles/preps on a verb (_svo_predicate_token /
--                          _svo_object_head): to/for/with/in/on/at/from/into/about/of. (mig 108)
--   * 'shell_noun'       — generic abstract/shell anaphoric heads (derive_sentence_facts._topic_
--                          definite_subject): "the flaw"/"the ruling"/"the condition" re-refer to the
--                          turn's discourse topic and bind back to it (cross-sentence coref). (mig 126)
-- The dependency relations (acl/compound/appos/dobj/pobj/prt/…) + POS function-word set stay in code;
-- ONLY the verb-LEMMA / particle-SURFACE vocabulary is DB data that grows (freq-gated, tenant-only).
-- Mirrors temporal_patterns lifecycle/confidence. Seeded (ALL categories, blanket SELECT) from
-- public.linguistic_cues at provisioning (schema_manager.py).
CREATE TABLE IF NOT EXISTS {schema_name}.linguistic_cues (
    id                SERIAL PRIMARY KEY,
    cue               VARCHAR(128) NOT NULL,
    category          VARCHAR(64)  NOT NULL DEFAULT 'naming_verb',
    frequency         INT   DEFAULT 0,
    confirmed_count   INT   DEFAULT 0,
    rejected_count    INT   DEFAULT 0,
    correction_count  INT   DEFAULT 0,
    global_confidence FLOAT DEFAULT 0.5,
    description       TEXT,
    example_text      TEXT,
    source            VARCHAR(64),
    is_active         BOOLEAN DEFAULT true,
    archived_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW(),
    last_matched_at   TIMESTAMP,
    UNIQUE (cue, category)
);
CREATE INDEX IF NOT EXISTS idx_linguistic_cues_active ON {schema_name}.linguistic_cues(is_active, category);
CREATE INDEX IF NOT EXISTS idx_linguistic_cues_category ON {schema_name}.linguistic_cues(category);

-- intent_pattern_cache: evictable TTL cache for Layer 2a intent pattern matching.
-- Seeded per-tenant so user memory is fully self-contained. (migration 066)
CREATE TABLE IF NOT EXISTS {schema_name}.intent_pattern_cache (
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
);
CREATE INDEX IF NOT EXISTS idx_intent_pattern_cache_lookup ON {schema_name}.intent_pattern_cache (user_id, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_intent_pattern_cache_eviction ON {schema_name}.intent_pattern_cache (expires_at) WHERE is_permanent = false;

-- ============================================================================
-- entity_synonyms — referential terms a USER uses to refer to an entity, or to
--   a relationship slot ("the wife" → spouse-filler). The LINGUISTIC layer
--   (how the user TALKS), kept STRICTLY separate from entity_aliases (the
--   DETERMINISTIC identity / proper-name layer). Synonyms are NEVER is_preferred
--   and NEVER render as a display name (registry display readers do not read
--   this table — verified registry.py:487/506/525).
--
-- DUPLICATES ALLOWED: a term may map to many entities (homonyms: "box" → many).
--   Only UNIQUE(term, entity_id) — no cross-entity uniqueness. Precision is a
--   READ-TIME concern (resolution disambiguator + refuse-on-tie), NOT a schema
--   constraint (SYNTHESIS #4).
--
-- Per-tenant: search_path has NO public, so this table MUST exist in every
--   tenant schema. NO public seed — synonyms are inherently user-specific
--   (created empty per tenant). Reads happen DIRECTLY under the bound schema;
--   NO overlay (there is no public seed to union — SYNTHESIS, PLAN-1 §7.5).
-- (migration 085)
-- ============================================================================
CREATE TABLE IF NOT EXISTS {schema_name}.entity_synonyms (
    id              SERIAL PRIMARY KEY,
    entity_id       TEXT NOT NULL,                          -- FK → entities.id (the referent)
    term            TEXT NOT NULL,                          -- NORMALIZED lexical key (see IMPL §5 contract)
    link_basis      TEXT NOT NULL DEFAULT 'entity',         -- 'entity' | 'relationship' (ontology-set, SYNTHESIS #9)
    role_rel_type   TEXT,                                   -- when link_basis='relationship': the rel whose
                                                            --   live filler the term resolves to (e.g. 'spouse',
                                                            --   'works_for'). NULL for 'entity'.
    -- precision / trust axis --------------------------------------------------
    source          TEXT NOT NULL DEFAULT 'user',           -- 'user' | 'extract' | 'llm_learned'
    fact_provenance TEXT NOT NULL DEFAULT 'user_stated',    -- mirrors facts provenance vocabulary
    confidence      FLOAT NOT NULL DEFAULT 1.0,
    approved        BOOLEAN NOT NULL DEFAULT true,          -- DEFAULT true ONLY because DEFAULT source='user';
                                                            --   inserter MUST set false for extract/llm_learned (full)
    occurrence_count INT NOT NULL DEFAULT 1,                -- frequency; reserved for the full promotion gate
    sensitivity     VARCHAR(50) NOT NULL DEFAULT 'normal',  -- 'normal' | 'sensitive'  (carried, no policy in pilot)
    -- lifecycle (non-destructive soft-delete, mirrors facts) ------------------
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at   TIMESTAMPTZ,                            -- soft-delete (DELETE / CORRECT). NULL = live.
    superseded_by   TEXT,                                   -- entity_id this term was re-pointed to (CORRECT)
    -- constraints -------------------------------------------------------------
    UNIQUE (term, entity_id),                               -- no dup of the SAME (term, entity) pair only
    FOREIGN KEY (entity_id) REFERENCES {schema_name}.entities(id) ON DELETE CASCADE,
    CONSTRAINT chk_es_link_basis  CHECK (link_basis IN ('entity','relationship')),
    CONSTRAINT chk_es_source      CHECK (source IN ('user','extract','llm_learned')),
    CONSTRAINT chk_es_provenance  CHECK (fact_provenance IN ('user_stated','llm_inferred','llm_learned')),
    CONSTRAINT chk_es_sensitivity CHECK (sensitivity IN ('normal','sensitive')),
    CONSTRAINT chk_es_role        CHECK (
        (link_basis = 'entity'       AND role_rel_type IS NULL) OR
        (link_basis = 'relationship' AND role_rel_type IS NOT NULL)
    )
);

-- term lookup (the resolution hot path): only live rows. NOT unique — homonyms allowed.
CREATE INDEX IF NOT EXISTS idx_entity_synonyms_term
    ON {schema_name}.entity_synonyms (term) WHERE superseded_at IS NULL;

-- per-entity sweep (CASCADE/merge/entity-scoped CRUD, and the density-count join in IMPL-3)
CREATE INDEX IF NOT EXISTS idx_entity_synonyms_entity
    ON {schema_name}.entity_synonyms (entity_id);

-- ============================================================================
-- episodic_log — durable, append-only, VERBATIM record of every ingest input,
--   captured BEFORE extraction. When extraction is weak (short fragments,
--   misrouted queries, no-triple ramblings) the original words are otherwise
--   lost forever. This is a pure safety net: it does NOT touch the WGM gate,
--   class assignment, or query scope, and writes to it must never block or
--   fail ingest. reextracted_at + the partial index support a future
--   re-mining backfill scan.
--
-- Per-tenant: search_path has NO public, so this table MUST exist in every
--   tenant schema. NO public seed — episodic rows are inherently user-specific
--   (created empty per tenant). user_id is stored for traceability (per the
--   entity_attributes/staged_facts convention); row isolation is provided by
--   the schema, not user_id scoping.
-- (migration 127 backfills this table into existing provisioned schemas)
-- ============================================================================
CREATE TABLE IF NOT EXISTS {schema_name}.episodic_log (
    id                   BIGSERIAL   PRIMARY KEY,
    user_id              TEXT        NOT NULL,
    raw_text             TEXT        NOT NULL,
    -- ORIGIN LANE — ⚠️ LOAD-BEARING FOR PROVENANCE (migration 206).
    -- reextract_episodic resolves the ORIGINAL /ingest source from this value, so it
    -- decides whether a re-mined fact comes back as USER TESTIMONY (Class A) or as
    -- ENGINE INFERENCE. Before that fix a user-STATED turn was re-ingested under
    -- source="reextract" and returned DEMOTED — the owner's ruling ("re-stated should
    -- be able to go from B→A; user is truth should be respected") is enforced HERE.
    --   'mcp'                    → chat turn, user-stated
    --   'document'               → document chunk, user-stated
    --   'store_context_deferred' → Class-C residue, NOT elevatable
    --   NULL / anything else     → origin unestablished, NOT elevatable (never guessed)
    -- A new writer MUST set this honestly — EpisodicAppendRequest.source DEFAULTS to
    -- 'mcp' (src/api/models.py), so omitting the field records the row as a user turn.
    source               TEXT,
    source_ref           TEXT,
    intent               TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    extracted_fact_count INTEGER     DEFAULT NULL,
    reextracted_at       TIMESTAMPTZ DEFAULT NULL,
    -- Migration 197 (ontology-regrowth marks). A zero-edge turn is stamped
    -- `reextracted_at = now(), extracted_fact_count = 0` and the backfill scans
    -- `WHERE reextracted_at IS NULL`, so it is frozen out PERMANENTLY — the growth engine never
    -- runs backwards over history. These two columns let a zero-edge turn be RE-OPENED once the
    -- tenant has grown enough walkable places to have somewhere to put it.
    -- ⚠️ MUST live here as well as in the migration: a migration-only change silently skips every
    -- NEWLY PROVISIONED tenant, which is exactly what happened when 197 shipped without this.
    reextract_ontology_mark  BIGINT  DEFAULT NULL,
    reextract_attempts       INTEGER NOT NULL DEFAULT 0,
    -- Migration 274 parity: per-turn idempotency key (client-generated, one per turn, shared by
    -- every retry attempt of that turn). The partial UNIQUE index below is what makes two
    -- in-flight append attempts converge on ONE row — a retry after a client-side ReadTimeout
    -- reads back the existing id instead of appending a duplicate. NULL for keyless writers.
    -- ⚠️ MUST live here as well as in the migration (a migration-only change skips new tenants).
    turn_key                 TEXT    DEFAULT NULL
);

-- Migration 274 parity: two attempts of one turn can never produce two rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_episodic_log_turn_key
    ON {schema_name}.episodic_log (turn_key) WHERE turn_key IS NOT NULL;

-- Partial index supporting the future re-extraction backfill scan.
CREATE INDEX IF NOT EXISTS idx_episodic_log_reextract
    ON {schema_name}.episodic_log (reextracted_at) WHERE reextracted_at IS NULL;

-- Chronological scan index.
CREATE INDEX IF NOT EXISTS idx_episodic_log_created_at
    ON {schema_name}.episodic_log (created_at);

-- Migration 197 parity: the ontology-regrowth re-mine scans zero-fact turns by their mark.
-- (Template-equivalence: a fresh mint swept by the full corpus must gain nothing.)
CREATE INDEX IF NOT EXISTS idx_episodic_log_regrowth
    ON {schema_name}.episodic_log (reextract_ontology_mark) WHERE extracted_fact_count = 0;

-- Migration 063 parity: Class-C hit-lifecycle decay/promotion scan.
CREATE INDEX IF NOT EXISTS idx_staged_facts_class_c_lifecycle
    ON {schema_name}.staged_facts (fact_class, expires_at, hit_count)
    WHERE fact_class = 'C' AND promoted_at IS NULL;

-- Migration 206 parity: the provenance contract must be discoverable on the column for a
-- NEWLY PROVISIONED tenant too, not just for the tenants the migration looped over. A
-- migration-only change silently skips every new tenant (the exact failure 197 shipped with).
COMMENT ON COLUMN {schema_name}.episodic_log.source IS
    'ORIGIN LANE of this retained turn. LOAD-BEARING: reextract_episodic resolves the '
    'original /ingest source from this value, so it decides whether a re-mined fact '
    'returns as USER TESTIMONY (Class A) or ENGINE INFERENCE. '
    '''mcp'' = chat turn (user-stated); ''document'' = document chunk (user-stated); '
    '''store_context_deferred'' = Class-C residue (NOT elevatable); '
    'NULL/unknown = origin unestablished (NOT elevatable -- never guessed). '
    'A new writer MUST set this honestly: EpisodicAppendRequest.source defaults to '
    '''mcp'', so omitting the field records the row as a user turn. See migration 206.';


-- ============================================================================
-- documents — per-tenant ASYNC document-ingestion registry.
--   The flagship `ingest_document` lane chunks a doc, writes ONE pending row here
--   (chunks retained verbatim in JSONB), and returns FAST. The re_embedder poll
--   loop drains pending rows, runs the per-chunk hybrid extraction (deterministic
--   spine FIRST + tenant-brain LLM /extract/rewrite for the sentences the spine
--   dropped) through the WGM gate (source="mcp" → user_stated, durable), and flips
--   status → 'ready'/'error'. Recall surfaces an honest "still processing" line
--   while a document is pending, without blocking recall of already-ready facts.
--   The chunks JSONB is the per-document verbatim safety net.
-- Per-tenant: search_path has NO public, so this table MUST exist in every tenant
--   schema. NO public seed — document rows are inherently user-specific.
-- (migration 183 backfills this table into existing provisioned schemas)
-- ============================================================================
CREATE TABLE IF NOT EXISTS {schema_name}.documents (
    id               BIGSERIAL   PRIMARY KEY,
    user_id          TEXT        NOT NULL,
    source_ref       TEXT,
    title            TEXT,
    chunks           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    chunk_count      INTEGER     NOT NULL DEFAULT 0,
    status           TEXT        NOT NULL DEFAULT 'pending',
    chunks_failed    INTEGER     NOT NULL DEFAULT 0,
    facts_committed  INTEGER     NOT NULL DEFAULT 0,
    facts_staged     INTEGER     NOT NULL DEFAULT 0,
    context_stored   INTEGER     NOT NULL DEFAULT 0,
    truncated        BOOLEAN     NOT NULL DEFAULT false,
    -- DOCLOSS part accounting (migration 195): an over-cap document is SEGMENTED across
    -- consecutive rows instead of having its tail discarded. These say which slice of the
    -- original document this row carries. Defaults = a legacy single-part document.
    part_index       INTEGER     NOT NULL DEFAULT 0,
    part_count       INTEGER     NOT NULL DEFAULT 1,
    total_chunks     INTEGER     NOT NULL DEFAULT 0,
    -- Bounded at-least-once redelivery: a brain-level outage re-pends the whole document;
    -- `attempts` stops a permanently-sick document re-pending forever (terminates 'error').
    attempts         INTEGER     NOT NULL DEFAULT 0,
    -- The deferral-episode WALL CLOCK (migration 264): set at the first non-paced
    -- brain-unavailable defer, cleared only on PROGRESS (terminal finalize or a
    -- PRODUCTIVE deadline requeue whose owed count decreased; an unproductive
    -- requeue keeps/stamps it, bounding the owed lane by the same episode — a claim
    -- does NOT clear it: a claim is a pure DB write that succeeds mid-outage).
    -- The terminal verdict
    -- for a document that cannot make progress is measured on THIS (one continuous
    -- episode, DOC_BRAIN_DEFER_MAX_AGE = 30 days, the system's own horizon for
    -- unprocessed user content), never on the attempt count — a count is outage
    -- duration sized by the drain interval, which killed innocent uploads ~5 minutes
    -- into hours-long outages (95fb517b measured; the decision was deferred then).
    brain_deferred_since TIMESTAMPTZ DEFAULT NULL,
    -- PARALLEL per-chunk terminal ledger (migration 205). Maps a chunk INDEX to its terminal
    -- state: {"7": {"s":"done","c":4,"g":1}} / {"7": {"s":"failed","a":3,"e":"..."}}.
    -- A chunk with NO entry has no terminal state and is therefore still OWED — that, not a
    -- counter, is what the parallel drain re-queues on reclaim. It is what makes a failed
    -- chunk RE-MINABLE instead of burned into a terminal 'partial'. '{}' = no parallel drain
    -- has run (the legacy lane never writes it).
    chunk_state      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ DEFAULT NULL,
    ready_at         TIMESTAMPTZ DEFAULT NULL
);

-- Partial index driving BOTH the worker drain and the recall not-ready probe.
CREATE INDEX IF NOT EXISTS idx_documents_active
    ON {schema_name}.documents (created_at) WHERE status IN ('pending', 'processing');


-- ============================================================================
-- artefacts — per-tenant ARTEFACT RETENTION (stage A) + caption binding (A.5).
--   Spec: the internal design record The same safety-net
--   principle as episodic_log (a turn) and documents.chunks (a document), applied
--   to BYTES: retain the artefact the user handed us, understand it later. Every
--   later understanding pass is then a pure ADDITION over a retained artefact.
--
--   `bytea`, NOT pg_largeobject: large objects are a per-DATABASE catalog addressed
--   by OID, so `search_path` gives them ZERO isolation, and DROP SCHEMA CASCADE
--   ORPHANS them instead of erasing them (a GDPR Art. 17 hole). `bytea` is erased by
--   the same schema drop as everything else — one boundary, one erasure surface.
--   `bytes` is STORAGE EXTERNAL (a PDF/JPEG is already entropy-coded; TOAST's default
--   compression attempt burns CPU for ~nothing).
--
--   `external_ref` ships unused ON PURPOSE — it is the pre-built object-storage flip
--   point, so outgrowing in-database storage is CONFIG, NOT A MIGRATION. Exactly one
--   of (bytes, external_ref) is set; ONE accessor resolves whichever it is
--   (src/ingest/artefacts.load_artefact_payload).
--
--   PROVENANCE / TIER (§4): the UPLOAD EVENT is the user's own act, nothing inferred →
--   user_stated / Class A (real memory before a pixel is read). The CAPTION binding is
--   the engine's geometry guess → Class B. Never C: C carries a 30-day expiry, and a
--   caption is TYPED fine — the engine is merely less sure it belongs to that image,
--   which is a confidence question (caption_confidence), not a classification failure.
--   The artefacts_caption_class_b CHECK makes the database refuse a Class-C caption.
--
--   Stage B (OCR / vision) is DEFERRED: ocr_* columns are reserved and nothing writes
--   them. ocr_text is NEVER merged into text_layer — different provenance, different
--   trust.
-- Per-tenant: search_path has NO public, so this table MUST exist in every tenant
--   schema. NO public seed — artefacts are inherently user-specific.
-- (migration 203 backfills this table into existing provisioned schemas)
-- ============================================================================
CREATE TABLE IF NOT EXISTS {schema_name}.artefacts (
    id               BIGSERIAL   PRIMARY KEY,
    user_id          TEXT        NOT NULL,
    -- the `documents` row this artefact arrived with, when it arrived with one.
    -- Deliberately NOT a FK: a retained artefact must survive its registry row.
    document_id      BIGINT,

    -- ── IDENTITY of the thing the user handed us (the UPLOAD EVENT) ─────────────
    filename         TEXT,
    media_type       TEXT        NOT NULL,
    byte_size        BIGINT      NOT NULL,
    sha256           TEXT        NOT NULL,
    page_index       INTEGER,          -- NULL = the whole file
    artefact_index   INTEGER,          -- NULL = the whole file; else ordinal on the page

    -- ── STORAGE: exactly one of these. The pre-built object-storage flip point ──
    bytes            BYTEA,
    external_ref     TEXT,

    -- ── CAPTURE PROVENANCE: grounded, not inferred ─────────────────────────────
    source_ref       TEXT,
    fact_provenance  TEXT        NOT NULL DEFAULT 'user_stated',
    fact_class       TEXT        NOT NULL DEFAULT 'A',
    captured_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ── PLACEMENT GEOMETRY: stage A.5 INPUT, read off the file itself ───────────
    -- top-origin (pdfplumber `top`/`bottom`) — ONE convention, recorded so a later
    -- reader cannot mix bottom-origin geometry from another library into it.
    page_width       REAL,
    page_height      REAL,
    bbox_x0          REAL,
    bbox_top         REAL,
    bbox_x1          REAL,
    bbox_bottom      REAL,

    -- ── ASSOCIATION: stage A.5 OUTPUT — engine inference. B, never A, never C ───
    caption_text            TEXT,
    caption_provenance      TEXT,
    caption_fact_class      TEXT,
    caption_confidence      REAL,
    caption_method          TEXT,
    caption_declined_reason TEXT,   -- WHY we refused to bind. A decline is a RESULT.
    caption_bound_at        TIMESTAMPTZ,

    -- ── CONTEXT TIE: which chunk of the document was this printed beside? (mig 208) ──
    -- `document_id` + `page_index` say which FILE and which PAGE. Neither says what was
    -- being SAID around the artefact — the mined prose lives in `documents.chunks`, a
    -- JSONB ARRAY, and facts are extracted per CHUNK. `chunk_index` is that array
    -- position, so document_id + chunk_index is a complete coordinate: this figure
    -- belongs beside THAT paragraph, and a walk reaching the paragraph reaches the figure.
    -- `chunk_bind_method` keeps the two ways apart instead of laundering a coarse guess
    -- into a precise one: 'caption' = the bound caption was found verbatim in that chunk;
    -- 'page_lead' = A.5 declined, so the page's first substantial line anchors it.
    -- Both NULL = neither resolved, recorded as unbound rather than fabricated.
    chunk_index         INTEGER,
    chunk_bind_method   TEXT,

    -- ── UNDERSTANDING: stage B, DEFERRED. Reserved; nothing writes these ────────
    text_layer       TEXT,
    ocr_text         TEXT,
    ocr_engine       TEXT,
    ocr_confidence   REAL,
    understood_at    TIMESTAMPTZ,

    CONSTRAINT artefacts_storage_xor
        CHECK (num_nonnulls(bytes, external_ref) <= 1),
    CONSTRAINT artefacts_byte_size_nonneg
        CHECK (byte_size >= 0),
    CONSTRAINT artefacts_identity_provenance
        CHECK (fact_provenance IN ('user_stated','llm_inferred','llm_learned')),
    CONSTRAINT artefacts_identity_class
        CHECK (fact_class IN ('A','B')),
    -- THE 30-DAY-FUSE GUARD: a caption may only ever be Class B.
    CONSTRAINT artefacts_caption_class_b
        CHECK (caption_fact_class IS NULL OR caption_fact_class = 'B'),
    CONSTRAINT artefacts_caption_needs_class
        CHECK (caption_text IS NULL OR caption_fact_class IS NOT NULL),
    -- A binding method may only be one we can justify, and never without an index to
    -- describe: the database refuses an unexplained tie (mig 208).
    CONSTRAINT artefacts_chunk_bind_method
        CHECK (chunk_bind_method IS NULL
               OR (chunk_index IS NOT NULL
                   AND chunk_bind_method IN ('caption','page_lead')))
);

ALTER TABLE {schema_name}.artefacts ALTER COLUMN bytes SET STORAGE EXTERNAL;

-- Dedup / "have I seen this before". COALESCE because NULLs are DISTINCT in a plain
-- UNIQUE, and page_index IS NULL is the common case — a plain constraint would let the
-- same 200-page PDF double-store silently.
CREATE UNIQUE INDEX IF NOT EXISTS uq_artefacts_identity
    ON {schema_name}.artefacts (user_id, sha256,
                                COALESCE(page_index, -1), COALESCE(artefact_index, -1));

-- Read path: "show me what came in with this document, in page order".
CREATE INDEX IF NOT EXISTS idx_artefacts_document
    ON {schema_name}.artefacts (document_id, page_index, artefact_index);

-- Work queue: artefacts A.5 has neither bound nor declined yet.
CREATE INDEX IF NOT EXISTS idx_artefacts_unassociated
    ON {schema_name}.artefacts (id)
    WHERE caption_text IS NULL AND caption_declined_reason IS NULL;

-- The READ the context tie exists for (mig 208): "what came in beside THIS chunk of THIS
-- document". Partial, so it stays small — most artefacts never resolve a chunk.
CREATE INDEX IF NOT EXISTS idx_artefacts_chunk
    ON {schema_name}.artefacts (document_id, chunk_index)
    WHERE chunk_index IS NOT NULL;
