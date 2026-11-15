-- 317_entity_attributes_valid_from_fan.sql — FILED-2 (SEC-BUG-3 closure addendum): THE
-- 011-FAN REPAIR — entity_attributes.valid_from reaches EVERY schema.
--
-- THE DEFECT (measured live on a fresh-mint seat schema, 2026-11-12)
-- ------------------------------------------------------------------
-- Migration 011 (bitemporal) added valid_from/valid_until to entity_attributes with a
-- BARE ALTER — whatever schema search_path pointed at when it ran (public), and nothing
-- else. Unlike 101 (datatype/unit/value_normalized) it never fanned to the tenant
-- schemas, and the provisioning template (src/provisioning/templates/user_schema.sql)
-- carried only valid_until — so EVERY fresh-mint schema was born one bitemporal half
-- short. The valid_from contract was broken on the fresh-mint lane for every consumer,
-- not just the serve lane that noticed (its savepoint fallback masks the missing column
-- — SEC-BUG-3's F5 attr guard degrades to the unguarded HEAD shape on fresh mints).
--
-- THE FIX (this file + the template column in the SAME commit — the 197/273/298/299/
-- 300/301 rule: template + migration, never one without the other)
-- ------------------------------------------------------------------
-- One ADDITIVE column, fanned to every schema that holds an entity_attributes table:
--
--     valid_from TIMESTAMPTZ   -- NULL = unbounded past (pre-011 semantics)
--
-- plus THE 034-HONORING STAMP: migration 034 set valid_from = created_at for the then-
-- existing public rows ("set valid_from to created_at for rows that lack valid_from —
-- pre-011 migration data"); this fan completes that same repair on every tenant schema
-- the 034 UPDATE never reached. The stamp is idempotent (WHERE valid_from IS NULL) and
-- serving-neutral by construction: every consumer guard reads
-- "valid_from IS NULL OR valid_from <= now()" and created_at is never in the future.
-- No row deletion, NO other metadata touched.
--
-- FAN-OUT: every schema holding an entity_attributes table (public + faultline_% +
-- flagger_% + pin/seat schemas), the 308/309/313/314 probe pattern (pg_namespace EXISTS
-- on the table name — the information_schema.schemata LIKE loop misses non-faultline_
-- prefixed schemas); the guarded per-schema loop skips a partially-provisioned schema
-- instead of aborting the whole DO block (the b99ce200 discipline: one bad schema must
-- never skip the rest). Idempotent: ADD COLUMN IF NOT EXISTS + the guarded UPDATE.
-- Safe to re-run.
DO $mig$
DECLARE
    _sch text;
BEGIN
    FOR _sch IN
        SELECT n.nspname
          FROM pg_namespace n
         WHERE EXISTS (
               SELECT 1
                 FROM pg_class c
                WHERE c.relnamespace = n.oid
                  AND c.relname = 'entity_attributes'
                  AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        BEGIN
            EXECUTE format('ALTER TABLE %I.entity_attributes ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ', _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;   -- dropped between probe and ALTER
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            -- THE 034-HONORING STAMP (idempotent): pre-existing rows carry their creation
            -- time as the validity start, exactly what 034 did for public. Rows a tenant
            -- writes AFTER this migration are stamped by the ingest lane (or stay NULL =
            -- unbounded past, the pre-011 reading every guard already accepts).
            EXECUTE format('UPDATE %I.entity_attributes SET valid_from = created_at WHERE valid_from IS NULL', _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;
            WHEN OTHERS THEN NULL;
        END;
    END LOOP;
END
$mig$;
