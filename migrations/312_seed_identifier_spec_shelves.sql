-- Migration 312: seed the identifier/spec shelves — `vmid`, `serial`, `model`, `spec`,
-- `part_number` (the evidenced-parity roadmap ITEM 3, competitive-eval 2026-11-10)
--
-- WHY
-- ---
-- The DETECTOR's identifier tier already captures the rows: IDENTIFIER_UNIT_SCALAR
-- (src/extraction/linguistics.py, BUG-FAMILY-2) emits (subject, <identifier head noun>,
-- "<bare integer>") for an unmapped compact technical head with a bare-integer magnitude
-- — "the VM for Home Assistant is vmid 112" lands (vm, vmid, "112") in entity_attributes
-- (measured on the family-smoke FIXED snapshot, DEV/BUGS/family-smoke-20261111). The gap
-- is the DECLARED vocabulary: the identifier head nouns have no rel_types row behind
-- them, so (a) each mint relies on the generic novel-rel path (metadata inferred per
-- seat, storage routing unstable) and (b) the READ side has no declared shelf to admit —
-- the recall's aspect binding misses open-domain spec nouns and the question
-- ("what is the vmid of the Home Assistant VM?" / "what's the home assistant vm id?")
-- cannot lay the row into the scalar lane (EVIDENCED-DIRECTIONS-TO-RAG-PARITY.md §3:
-- "landed, not served"). This migration seeds the closed identifier head-noun class the
-- detector's tier names — vmid / serial / model / spec / part_number (the underscore form
-- is the attribute-key spelling of "part number") — so the arm and the READ side both
-- hold:
--   * the WRITE side: an edge with rel_type <shelf> + object_datatype routes to the
--     scalar store (entity_attributes) under the CANONICAL attribute name (_canonical_
--     scalar_attribute_key: identity no-op for a known rel_type);
--   * the READ side: determine_path's ATTRSCOPE admission joins
--     entity_attributes.attribute = rel_types.rel_type AND requires tail_types to hold
--     SCALAR — an attribute with no rel_types row behind it is "name-shaped residue" and
--     is never admitted, so an unseeded shelf would mint rows recall can never walk to.
-- Without the seed the WGM gate would route every such edge through the novel-rel
-- fallback (statements classification unstable) — the vocabulary is grown PROPERLY, here.
-- The walk-side SERVING admission (query names a declared shelf surface + the anchored
-- entity holds a live row on it → the shelf lays into path.scalar_rels and the #13
-- exemption admits it) rides the RECALL_SPEC_SHELF flag in src/api/main.py, the
-- schedule/rule-register admissions' twin.
--
-- WHAT
-- ----
-- Five rows, modeled end-to-end on migrations 310/311's contract (category 'system',
-- tail_types {SCALAR}, scalar_datatype 'string' — "112", "XR7-9920", "23C" are opaque
-- strings at the scalar store; the ctx-bench scorer lowercases and substring-matches gold
-- tokens, so the storage contract is score-compatible):
--   vmid        — 'X vmid is Y'   (the VM/instance identifier, "vmid 112");
--   serial      — 'X serial is Y' (the hardware serial, "serial number DN82147");
--   model       — 'X model is Y'  (the model designator, "a Beseler 23C");
--   spec        — 'X spec is Y'   (the spec/designation, "the 0W-20 spec");
--   part_number — 'X part number is Y' (the part designator, "Honda 15400-PLM-A02").
-- correction_behavior supersede on every shelf (a re-stated identifier supersedes the
-- old row through SS1 — an identifier is a single-truth slot). With 275's full metadata
-- contract: source builtin, fact_class B, storage_target facts, natural_language_2p,
-- temporal_class state, is_leaf_only true. Confidence 0.8 (a deterministic seeded shelf,
-- the 310/311 floor).
--
-- WHY A NEW LATE FILE (275's discipline, same as 310/311): a fresh late file is applied
-- once, sorted after all the shapers, and lands the identical row a fresh first boot
-- does. Idempotent: ON CONFLICT DO NOTHING everywhere — a tenant that already grew its
-- own vmid/serial/model/spec/part_number keeps its row.

-- ── 1. public (the template / seed source for every future tenant) ──────────
INSERT INTO public.rel_types
    (rel_type, label, wikidata_pid, head_types, tail_types, engine_generated, confidence,
     correction_behavior, source, category, is_symmetric, inverse_rel_type, is_leaf_only,
     is_hierarchy_rel, storage_target, fact_class, natural_language_2p, temporal_class,
     scalar_datatype, value_min, value_max)
VALUES
    ('vmid', 'Vmid', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'Your vmid is Y', 'state',
     'string', NULL, NULL),
    ('serial', 'Serial', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'Your serial is Y', 'state',
     'string', NULL, NULL),
    ('model', 'Model', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'Your model is Y', 'state',
     'string', NULL, NULL),
    ('spec', 'Spec', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'Your spec is Y', 'state',
     'string', NULL, NULL),
    ('part_number', 'Part Number', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
     'supersede', 'builtin', 'system', false, NULL, true,
     false, 'facts', 'B', 'Your part number is Y', 'state',
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
                    ('vmid', 'Vmid', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'Your vmid is Y', 'state',
                     'string', NULL, NULL),
                    ('serial', 'Serial', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'Your serial is Y', 'state',
                     'string', NULL, NULL),
                    ('model', 'Model', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'Your model is Y', 'state',
                     'string', NULL, NULL),
                    ('spec', 'Spec', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'Your spec is Y', 'state',
                     'string', NULL, NULL),
                    ('part_number', 'Part Number', NULL, NULL, ARRAY['SCALAR']::TEXT[], false, 0.8,
                     'supersede', 'builtin', 'system', false, NULL, true,
                     false, 'facts', 'B', 'Your part number is Y', 'state',
                     'string', NULL, NULL)
                ON CONFLICT (rel_type) DO NOTHING
            $ins$, _schema);
        END IF;
    END LOOP;
END $$;
