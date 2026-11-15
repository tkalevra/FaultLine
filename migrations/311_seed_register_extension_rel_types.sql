-- Migration 311: seed the instruction-register extension shelves — `expires_on` and
-- `account_rep` (BUG-RECALL-7 extension, recall-servicing 2026-11-10)
--
-- WHY
-- ---
-- The instruction-register arm (BUG-RECALL-7 part B, migration 310) grew past its one
-- shelf: the corpus-A L-tier misses that remain are frames whose honest attribute is NOT
-- a setting value — "the TLS certificate for bramble.internal expires March 3" is an
-- EXPIRY DATE, and "Our vendor is Northlake Cloud, account rep is Fenna" is a VENDOR
-- RELATION. Minting them as has_setting would strain the attribute's semantics and leave
-- the natural question ("when does the certificate expire?") answering from a mislabeled
-- row. The detector (``INSTRUCTION_REGISTER_SETTING`` in src/extraction/linguistics.py)
-- now declares the shelf per frame; this migration seeds the two new shelves so the arm
-- and the READ side both hold:
--   * the WRITE side: an edge with rel_type expires_on/account_rep + object_datatype
--     routes to the scalar store (entity_attributes) under the canonical attribute name
--     (identity no-op for a known rel_type);
--   * the READ side: determine_path's ATTRSCOPE admission joins
--     entity_attributes.attribute = rel_types.rel_type AND requires tail_types to hold
--     SCALAR — an attribute with no rel_types row behind it is never admitted, so an
--     unseeded shelf would mint rows recall can never walk to.
-- Without the seed the WGM gate would refuse every edge the arm mints on these shelves
-- (statements_dropped) — the arm never bypasses the gate; the vocabulary is grown
-- PROPERLY, here.
--
-- WHAT
-- ----
-- Two rows, modeled end-to-end on migration 310's has_setting contract (category
-- 'system', tail_types {SCALAR}, scalar_datatype 'string' — "March 3" and a person name
-- are opaque strings at the scalar store, which lowercases values; the ctx-bench scorer
-- lowercases gold tokens too, so the storage contract is score-compatible):
--   expires_on  — 'X expires on Y' (cert/lease/trial expiry dates; correction_behavior
--                 supersede: a renewed expiry supersedes the old row through SS1);
--   account_rep — 'X account rep is Y' (the vendor-entity → person-name relation).
-- With 275's full metadata contract: source builtin, fact_class B, storage_target facts,
-- natural_language_2p, temporal_class state, is_leaf_only true. Confidence 0.8 (a
-- deterministic seeded shelf, same floor as 310).
--
-- WHY A NEW LATE FILE (275's discipline, same as 310): a fresh late file is applied once,
-- sorted after all the shapers, and lands the identical row a fresh first boot does.
-- Idempotent: ON CONFLICT DO NOTHING everywhere — a tenant that already grew its own
-- expires_on/account_rep keeps its row.

-- ── 1. public (the template / seed source for every future tenant) ──────────
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, head_types, tail_types, engine_generated, confidence,
     correction_behavior, source, category, is_symmetric, inverse_rel_type, is_leaf_only,
     is_hierarchy_rel, storage_target, fact_class, natural_language_2p, temporal_class,
     scalar_datatype, value_min, value_max)
VALUES
    ('expires_on', 'Expires On', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'X expires on Y', 'state',
     'string', NULL, NULL),
    ('account_rep', 'Account Rep', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'X account rep is Y', 'state',
     'string', NULL, NULL)
ON CONFLICT (rel_type) DO NOTHING;

-- ── 2. every already-provisioned tenant schema (same rows, never an override) ─
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
                    ('expires_on', 'Expires On', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'X expires on Y', 'state',
                     'string', NULL, NULL),
                    ('account_rep', 'Account Rep', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'X account rep is Y', 'state',
                     'string', NULL, NULL)
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);
        END IF;
    END LOOP;
END $$;
