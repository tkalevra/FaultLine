-- Migration 101: SCALAR-TYPE discipline — datatype as a first-class metadata property
-- Date: 2026-06-19
-- Purpose: Make the DATATYPE of a SCALAR slot a first-class metadata property of the
-- rel_type (validation + retrieval), and persist the detected datatype onto each stored
-- scalar in entity_attributes. Same move already made for relations (rel_types) and for
-- temporal (event_date + granularity, migration 098 — the proven template this mirrors).
--
-- THE HARD LINE
-- -------------
-- Scalars stay grounded LEAF MEMORIES in entity_attributes — typed for VALIDATION and
-- RETRIEVAL, NEVER L4-placed, NEVER vector-indexed. This migration adds the datatype tag;
-- it does NOT change where scalars live.
--
-- WHAT
-- ----
-- 1. rel_types gains:  scalar_datatype TEXT NULL  (the closed XSD/Wikidata-literal set),
--                      value_min DOUBLE PRECISION NULL, value_max DOUBLE PRECISION NULL,
--                      unit TEXT NULL  (range + canonical unit for quantity datatypes).
-- 2. entity_attributes gains: datatype TEXT NULL, unit TEXT NULL, value_normalized TEXT NULL.
-- 3. scalar_datatype seeded on existing SCALAR rel_types (tail_types && ARRAY['SCALAR'])
--    from a CLOSED, subject-agnostic set modeled on XSD / Wikidata literal datatypes.
--    age → integer (min 0, max 150 for Person head), born_on → date, height/weight →
--    quantity, has_ip → ipv4, has_mac → mac, has_email → email, etc.
--
-- CLOSED DATATYPE SET:
--   integer, decimal, quantity, date, datetime, string, ipv4, ipv6, cidr, mac, email,
--   url, fqdn, phone, uuid, boolean, currency, percentage, coordinate, duration
--
-- Additive + back-compatible (ADD COLUMN IF NOT EXISTS; named CHECK added only if absent).
-- public is the SEED SOURCE / TEMPLATE ONLY. Idempotent. Mirrors migration 098's shape:
-- public first, then fan out to every faultline_% tenant schema. New tenants inherit via
-- user_schema.sql (the column-set + CHECK are appended there too) and the bootstrap
-- `INSERT INTO rel_types SELECT * FROM public.rel_types` (columns appended LAST = ordinal-safe).

-- Closed datatype set, reused by every CHECK below.
--   integer decimal quantity date datetime string ipv4 ipv6 cidr mac email url fqdn
--   phone uuid boolean currency percentage coordinate duration

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.rel_types
    ADD COLUMN IF NOT EXISTS scalar_datatype TEXT NULL;
ALTER TABLE public.rel_types
    ADD COLUMN IF NOT EXISTS value_min DOUBLE PRECISION NULL;
ALTER TABLE public.rel_types
    ADD COLUMN IF NOT EXISTS value_max DOUBLE PRECISION NULL;
ALTER TABLE public.rel_types
    ADD COLUMN IF NOT EXISTS unit TEXT NULL;

ALTER TABLE public.entity_attributes
    ADD COLUMN IF NOT EXISTS datatype TEXT NULL;
ALTER TABLE public.entity_attributes
    ADD COLUMN IF NOT EXISTS unit TEXT NULL;
ALTER TABLE public.entity_attributes
    ADD COLUMN IF NOT EXISTS value_normalized TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_rel_types_scalar_datatype'
          AND conrelid = 'public.rel_types'::regclass
    ) THEN
        ALTER TABLE public.rel_types
            ADD CONSTRAINT chk_rel_types_scalar_datatype
            CHECK (scalar_datatype IS NULL OR scalar_datatype IN (
                'integer','decimal','quantity','date','datetime','string','ipv4','ipv6',
                'cidr','mac','email','url','fqdn','phone','uuid','boolean','currency',
                'percentage','coordinate','duration'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_entity_attributes_datatype'
          AND conrelid = 'public.entity_attributes'::regclass
    ) THEN
        ALTER TABLE public.entity_attributes
            ADD CONSTRAINT chk_entity_attributes_datatype
            CHECK (datatype IS NULL OR datatype IN (
                'integer','decimal','quantity','date','datetime','string','ipv4','ipv6',
                'cidr','mac','email','url','fqdn','phone','uuid','boolean','currency',
                'percentage','coordinate','duration'));
    END IF;
END $$;

-- ── 2. Seed scalar_datatype on existing SCALAR rel_types (public) ────────────
-- Closed, subject-agnostic mapping by canonical rel_type name. Any SCALAR rel NOT named
-- here falls back to 'string' so it is still typed (never NULL-on-a-SCALAR). Only writes
-- where scalar_datatype IS NULL (idempotent — re-running never clobbers a curated value).
UPDATE public.rel_types
SET scalar_datatype = CASE rel_type
        WHEN 'has_ip'       THEN 'ipv4'
        WHEN 'has_mac'      THEN 'mac'
        WHEN 'has_email'    THEN 'email'
        WHEN 'has_url'      THEN 'url'
        WHEN 'has_fqdn'     THEN 'fqdn'
        WHEN 'fqdn'         THEN 'fqdn'
        WHEN 'has_hostname' THEN 'fqdn'
        WHEN 'hostname'     THEN 'fqdn'
        WHEN 'has_subnet'   THEN 'cidr'
        WHEN 'has_phone'    THEN 'phone'
        WHEN 'has_uuid'     THEN 'uuid'
        WHEN 'has_port'     THEN 'integer'
        WHEN 'located_at'   THEN 'string'
        WHEN 'age'          THEN 'integer'
        WHEN 'born_on'      THEN 'date'
        WHEN 'height'       THEN 'quantity'
        WHEN 'weight'       THEN 'quantity'
        WHEN 'has_gender'   THEN 'string'
        WHEN 'nationality'  THEN 'string'
        WHEN 'occupation'   THEN 'string'
        ELSE 'string'
    END
WHERE scalar_datatype IS NULL
  AND tail_types && ARRAY['SCALAR']::TEXT[];

-- Age realism range (subject-agnostic on the slot; the Person 0–150 cap is enforced at
-- ingest only when the head entity is a Person — non-Person ages just need >= 0). The
-- slot-level floor lives in metadata: integer, min 0. The Person upper bound is a head-
-- type-conditioned ingest rule (validator registry), so value_max stays NULL here to avoid
-- wrongly capping non-Person ages.
UPDATE public.rel_types
SET value_min = 0
WHERE rel_type = 'age' AND value_min IS NULL;

-- ── 3. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format('ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS scalar_datatype TEXT NULL', _schema);
        EXECUTE format('ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS value_min DOUBLE PRECISION NULL', _schema);
        EXECUTE format('ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS value_max DOUBLE PRECISION NULL', _schema);
        EXECUTE format('ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS unit TEXT NULL', _schema);

        EXECUTE format('ALTER TABLE %I.entity_attributes ADD COLUMN IF NOT EXISTS datatype TEXT NULL', _schema);
        EXECUTE format('ALTER TABLE %I.entity_attributes ADD COLUMN IF NOT EXISTS unit TEXT NULL', _schema);
        EXECUTE format('ALTER TABLE %I.entity_attributes ADD COLUMN IF NOT EXISTS value_normalized TEXT NULL', _schema);

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_rel_types_scalar_datatype'
              AND conrelid = format('%I.rel_types', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.rel_types ADD CONSTRAINT chk_rel_types_scalar_datatype '
                'CHECK (scalar_datatype IS NULL OR scalar_datatype IN ('
                '''integer'',''decimal'',''quantity'',''date'',''datetime'',''string'',''ipv4'',''ipv6'','
                '''cidr'',''mac'',''email'',''url'',''fqdn'',''phone'',''uuid'',''boolean'',''currency'','
                '''percentage'',''coordinate'',''duration''))',
                _schema);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_entity_attributes_datatype'
              AND conrelid = format('%I.entity_attributes', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.entity_attributes ADD CONSTRAINT chk_entity_attributes_datatype '
                'CHECK (datatype IS NULL OR datatype IN ('
                '''integer'',''decimal'',''quantity'',''date'',''datetime'',''string'',''ipv4'',''ipv6'','
                '''cidr'',''mac'',''email'',''url'',''fqdn'',''phone'',''uuid'',''boolean'',''currency'','
                '''percentage'',''coordinate'',''duration''))',
                _schema);
        END IF;

        -- Seed scalar_datatype on the tenant's SCALAR rel_types (same closed map).
        EXECUTE format($q$
            UPDATE %I.rel_types
            SET scalar_datatype = CASE rel_type
                    WHEN 'has_ip'       THEN 'ipv4'
                    WHEN 'has_mac'      THEN 'mac'
                    WHEN 'has_email'    THEN 'email'
                    WHEN 'has_url'      THEN 'url'
                    WHEN 'has_fqdn'     THEN 'fqdn'
                    WHEN 'fqdn'         THEN 'fqdn'
                    WHEN 'has_hostname' THEN 'fqdn'
                    WHEN 'hostname'     THEN 'fqdn'
                    WHEN 'has_subnet'   THEN 'cidr'
                    WHEN 'has_phone'    THEN 'phone'
                    WHEN 'has_uuid'     THEN 'uuid'
                    WHEN 'has_port'     THEN 'integer'
                    WHEN 'located_at'   THEN 'string'
                    WHEN 'age'          THEN 'integer'
                    WHEN 'born_on'      THEN 'date'
                    WHEN 'height'       THEN 'quantity'
                    WHEN 'weight'       THEN 'quantity'
                    WHEN 'has_gender'   THEN 'string'
                    WHEN 'nationality'  THEN 'string'
                    WHEN 'occupation'   THEN 'string'
                    ELSE 'string'
                END
            WHERE scalar_datatype IS NULL
              AND tail_types && ARRAY['SCALAR']::TEXT[]
        $q$, _schema);

        EXECUTE format(
            'UPDATE %I.rel_types SET value_min = 0 WHERE rel_type = ''age'' AND value_min IS NULL',
            _schema);

        RAISE NOTICE 'Migration 101: scalar_datatype discipline applied to %', _schema;
    END LOOP;
END $$;
