-- Migration 275: seed the `has_port` scalar rel_type — the one 060 row 061 never carried
-- Date: 2026-09-16 (gauntlet first-boot-migration-corpus)
--
-- WHY
-- ---
-- Migration 060 seeded the atomic scalar patterns (has_ip, has_mac, has_email, has_phone,
-- has_port, has_url, has_uuid, has_subnet …) and then tried to seed the matching rel_types with
-- `source = 'bootstrap'` — a value `rel_types_source_check` has NEVER admitted (007:
-- wikidata|builtin|engine|user; 072/073 add expand). PostgreSQL rejected that INSERT as a unit
-- (SQLSTATE 23514) on EVERY boot of EVERY box. Six of its seven rels were re-seeded properly by
-- 061_fix_networking_rel_types.sql (source builtin, tail_types {SCALAR}, fact_class B, category
-- network) and later typed by 081 (natural_language_2p) and 101 (scalar_datatype). `has_port` is
-- the one rel 061 does not list, so it has never existed in public.rel_types on any box —
-- measured on a three-boot database: the has_port PATTERN rows exist, the has_port REL does not.
-- The pattern layer therefore emits `has_port` edges into a rel the ontology does not know, and
-- each tenant is left to mint it through the growth path (engine, class C, untyped tail — a port
-- number filed as a UUID entity instead of a SCALAR string).
--
-- WHAT
-- ----
-- Seed `has_port` with exactly the metadata its siblings received end-to-end (061 + 081 + 101):
-- source builtin, tail_types {SCALAR}, category network, fact_class B, is_leaf_only true,
-- storage_target facts, temporal_class state, scalar_datatype integer (101's CASE arm for
-- has_port — RFC 6335 port range 1–65535 as value_min/value_max), natural_language_2p in 081's
-- shape. Confidence 0.7 (061's floor for the un-refined networking scalars).
--
-- WHY A NEW LATE FILE AND NOT A FIX INSIDE 060: 060 is an early seed whose rows several later
-- migrations CORRECT by UPDATE (184 has_phone, 198 has_port); under the boot-migration ledger an
-- edited file re-runs once on every migrated box, and 060's ON CONFLICT DO NOTHING pattern seed
-- then re-inserts the pre-correction rows beside the corrected ones (that is exactly what its
-- perpetual `failed` re-run has been doing — see 276). The rel_types INSERT is deleted from
-- 060 with the reason; this file carries the one row that has nowhere else to live, sorted after
-- 101 so scalar_datatype can be set here directly and a fresh first boot lands the same row a
-- migrated box does. Idempotent: ON CONFLICT DO NOTHING everywhere — a tenant that already grew
-- its own `has_port` keeps its row (the seed never overrides a tenant's table; authority order
-- user > seed > growth is about VALUES, and a DO NOTHING seed is the conservative side of the
-- "hands off an existing tenant" ruling).

-- ── 1. public (the template / seed source for every future tenant) ──────────
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, head_types, tail_types, engine_generated, confidence,
     correction_behavior, source, category, is_symmetric, inverse_rel_type, is_leaf_only,
     is_hierarchy_rel, storage_target, fact_class, natural_language_2p, temporal_class,
     scalar_datatype, value_min, value_max)
VALUES
    ('has_port', 'Has Port', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.7,
     'supersede', 'builtin', 'network', false, NULL, true,
     false, 'facts', 'B', 'You have port Y', 'state',
     'integer', 1, 65535)
ON CONFLICT (rel_type) DO NOTHING;

-- ── 2. every already-provisioned tenant schema (same row, never an override) ─
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = _schema AND table_name = 'rel_types'
              AND column_name = 'scalar_datatype'
        ) THEN
            EXECUTE format($ins$
                INSERT INTO %I.rel_types
                    (rel_type, label, wikidata_pid, head_types, tail_types, engine_generated,
                     confidence, correction_behavior, source, category, is_symmetric,
                     inverse_rel_type, is_leaf_only, is_hierarchy_rel, storage_target,
                     fact_class, natural_language_2p, temporal_class, scalar_datatype,
                     value_min, value_max)
                VALUES
                    ('has_port', 'Has Port', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.7,
                     'supersede', 'builtin', 'network', false, NULL, true,
                     false, 'facts', 'B', 'You have port Y', 'state',
                     'integer', 1, 65535)
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);
        END IF;
    END LOOP;
END $$;
