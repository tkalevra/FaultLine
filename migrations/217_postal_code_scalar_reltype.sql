-- Migration 217: type postal_code as a SCALAR rel_type (was engine-grown mis-typed)
-- Date: 2026-08-04
--
-- WHY
-- ---
-- `postal_code` grew engine-side (source='engine') as a RELATIONAL rel_type with
-- tail_types=['ANY'], scalar_datatype=NULL, category='pending_placement' — i.e. it never
-- matured into a scalar. Consequence (measured on a live seat): every statement of a postal
-- code ("my postal code is X9K 8Z7") stored the value as a RELATIONAL fact whose object was
-- a freshly-minted Location ENTITY (aliased to a truncated fragment "x9"), instead of a
-- scalar string in entity_attributes. Recall then had nothing coherent to surface (broken
-- entity + out-of-scope pending_placement rel), and each LLM-routed correction made it
-- WORSE (x9k 8z7 → x9k → x9 → nothing). The rel_type typing is the root cause: a postal
-- code is a SCALAR literal, never an entity reference.
--
-- FIX (metadata-driven, subject-agnostic — NO domain literals in Python)
-- ------------------------------------------------------------------------------------
-- Promote postal_code to a proper SCALAR rel_type mirroring has_reference_id / lives_at:
-- tail_types={SCALAR}, scalar_datatype='string', fact_class='B', category='location'.
-- Future statements route the value to entity_attributes exactly like has_reference_id /
-- has_email / has_ip. Seeds public.rel_types (NEW tenants inherit via provisioning) AND
-- UPDATEs every already-provisioned faultline_% tenant that grew the mis-typed row.
-- Idempotent: ON CONFLICT (rel_type) DO UPDATE; no DROP, no data loss.
--
-- NOTE: this fixes the TYPE so future writes land as scalars. It does not migrate the
-- EXISTING broken rows (a `(user, postal_code, <entity-uuid>)` fact is still relational) —
-- those are cleaned up surgically per-seat after deploy, or simply superseded by a fresh
-- correct statement through the now-fixed path.

-- ── public seed (NEW tenants inherit via provisioning copy) ──────────────────
INSERT INTO public.rel_types
    (rel_type, label, head_types, tail_types, is_symmetric, is_hierarchy_rel,
     category, fact_class, scalar_datatype, engine_generated, source, confidence, natural_language)
VALUES
    ('postal_code', 'Postal code', ARRAY['ANY'], ARRAY['SCALAR'],
     false, false, 'location', 'B', 'string', true, 'builtin', 0.85, 'X has the postal code Y')
ON CONFLICT (rel_type) DO UPDATE SET
    label            = EXCLUDED.label,
    head_types       = EXCLUDED.head_types,
    tail_types       = EXCLUDED.tail_types,
    is_hierarchy_rel = EXCLUDED.is_hierarchy_rel,
    category         = EXCLUDED.category,
    fact_class       = EXCLUDED.fact_class,
    scalar_datatype  = EXCLUDED.scalar_datatype,
    source           = EXCLUDED.source,
    confidence       = EXCLUDED.confidence,
    natural_language = EXCLUDED.natural_language;

-- ── backfill / re-type every already-provisioned tenant schema ───────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($f$
            INSERT INTO %I.rel_types
                (rel_type, label, head_types, tail_types, is_symmetric, is_hierarchy_rel,
                 category, fact_class, scalar_datatype, engine_generated, source, confidence, natural_language)
            VALUES
                ('postal_code', 'Postal code', ARRAY['ANY'], ARRAY['SCALAR'],
                 false, false, 'location', 'B', 'string', true, 'builtin', 0.85, 'X has the postal code Y')
            ON CONFLICT (rel_type) DO UPDATE SET
                label            = EXCLUDED.label,
                head_types       = EXCLUDED.head_types,
                tail_types       = EXCLUDED.tail_types,
                is_hierarchy_rel = EXCLUDED.is_hierarchy_rel,
                category         = EXCLUDED.category,
                fact_class       = EXCLUDED.fact_class,
                scalar_datatype  = EXCLUDED.scalar_datatype,
                source           = EXCLUDED.source,
                confidence       = EXCLUDED.confidence,
                natural_language = EXCLUDED.natural_language
        $f$, _schema);
    END LOOP;
END $$;
