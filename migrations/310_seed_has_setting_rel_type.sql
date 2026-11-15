-- Migration 310: seed the `has_setting` scalar rel_type — the INSTRUCTION-REGISTER shelf
-- (BUG-RECALL-7 part B, recall-servicing 2026-11-09)
--
-- WHY
-- ---
-- BUG-RECALL-7 measured the class: prescription/instruction sentences ("you need the
-- --force-recreate flag", "Set Redis maxmemory to 512mb", "the health probe wants the
-- X-Health-Token header", "always pg_dump with --clean --if-exists") are captured VERBATIM in
-- episodic but mint ZERO entity_attributes rows — the extraction has no arm for the register,
-- so the ops corpus's command_flag/config_value questions (8-9 of the 19 user-lane L misses,
-- the single largest miss class) have no fact to serve. The arm
-- (``INSTRUCTION_REGISTER_SETTING`` in src/extraction/linguistics.py, same commit) mints
-- (tool/flow subject, has_setting, value) scalar edges through the SAME seam every other
-- deterministic deriver arm uses. This migration seeds the shelf the arm and the READ side
-- both require:
--   * the WRITE side: an edge with rel_type has_setting + object_datatype routes to the
--     scalar store (entity_attributes) and files the attribute under the CANONICAL shelf
--     name (_canonical_scalar_attribute_key rule 2: identity no-op for a known rel_type);
--   * the READ side: determine_path's ATTRSCOPE admission joins
--     entity_attributes.attribute = rel_types.rel_type AND requires tail_types to hold
--     SCALAR — an attribute with no rel_types row behind it is "name-shaped residue" and is
--     never admitted, so an unseeded arm shelf would mint rows recall can never walk to.
-- Without the seed the WGM gate would refuse every edge the arm mints (statements_dropped)
-- — the arm never bypasses the gate; the vocabulary is grown PROPERLY, here.
--
-- WHAT
-- ----
-- One row, modeled end-to-end on the has_os sibling (category 'system', tail_types {SCALAR},
-- scalar_datatype 'string' — the flag/setting values are opaque strings: "--force-recreate",
-- "512mb", "X-Health-Token", "--workers 4", "release/") with 275's full metadata contract:
-- source builtin, fact_class B, storage_target facts, correction_behavior supersede,
-- natural_language_2p in 081's shape, temporal_class state, is_leaf_only true. Confidence 0.8
-- (a deterministic seeded shelf, same floor as 061's refined scalars).
--
-- WHY A NEW LATE FILE (275's discipline): early seed files are corrected by later UPDATEs
-- and re-run on every migrated box; a fresh late file is applied once, sorted after all the
-- shapers, and lands the identical row a fresh first boot does. Idempotent: ON CONFLICT DO
-- NOTHING everywhere — a tenant that already grew its own `has_setting` keeps its row
-- (authority order user > seed > growth is about VALUES; a DO NOTHING seed is the
-- conservative side of the "hands off an existing tenant" ruling).

-- ── 1. public (the template / seed source for every future tenant) ──────────
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, head_types, tail_types, engine_generated, confidence,
     correction_behavior, source, category, is_symmetric, inverse_rel_type, is_leaf_only,
     is_hierarchy_rel, storage_target, fact_class, natural_language_2p, temporal_class,
     scalar_datatype, value_min, value_max)
VALUES
    ('has_setting', 'Has Setting', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'You have setting Y', 'state',
     'string', NULL, NULL)
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
                    ('has_setting', 'Has Setting', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'You have setting Y', 'state',
                     'string', NULL, NULL)
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);
        END IF;
    END LOOP;
END $$;
