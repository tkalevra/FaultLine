-- Migration 089: wire the dormant hierarchy flags on rel_types
-- Date: 2026-06-14
-- Purpose: SCHEMA FOUNDATION for the hierarchy ladder (rung 4 + transitive trace-back).
--
-- WHAT
-- ----
-- The hierarchy rel_types already exist but their structural flags were left unset, so
-- traversal never walks them. This wires the CLOSED, CURATED hierarchy set:
--   hierarchy rels (is_hierarchy_rel=true): instance_of, is_a, member_of, part_of, subclass_of
--   transitivity is an entity_taxonomies property (019), NOT a rel_types column — see the note
--   below the first UPDATE for why the two transitivity UPDATEs this file used to carry are gone.
-- ("my animals" walks DOWN transitive subclass_of to Rex; instance_of is a single hop.)
--
-- These are guarded UPDATEs against EXISTING curated rel_types — they touch nothing else and
-- never mint a rel. This is NOT keyword-promotion of novel rels (which stays disabled per the
-- design); it only flips the flags on the known closed hierarchy set.
--
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: pure UPDATEs, re-runnable.
-- NOTE: schema_manager bootstrap copies these columns from public on provisioning
--       (is_hierarchy_rel via the DO UPDATE set), so NEW tenants inherit the wired flags;
--       this migration also fans out to EXISTING tenants for parity.

-- ── 1. public (the template / seed source) ─────────────────────────────────
UPDATE public.rel_types
   SET is_hierarchy_rel = true
 WHERE rel_type IN ('instance_of', 'is_a', 'member_of', 'part_of', 'subclass_of');

-- TWO `has_transitivity` / `transitive_rel_types` UPDATEs REMOVED HERE, and the matching two
-- EXECUTEs in the fan-out below (gauntlet first-boot-migration-corpus, 2026-09-16). They
-- targeted `public.rel_types.has_transitivity` — a column that has NEVER existed on rel_types
-- in any migration or in the template. `has_transitivity` / `transitive_rel_types` live on
-- `entity_taxonomies` (019, seeded there; read by taxonomy_overlay.py and nowhere else) and the
-- design this file cites wires them THERE. Nothing in src/ reads a rel_types transitivity
-- column. Result on every box: SQLSTATE 42703 on both statements on EVERY boot, the ledger
-- recorded this file `failed` forever and re-ran it on every start — and because the fan-out
-- DO block below EXECUTEd the same broken UPDATE, the WHOLE block rolled back each time, so the
-- per-tenant is_hierarchy_rel wiring it promised never landed on a single existing tenant.
-- Adding the column to satisfy the statement would mint an unread column; the taxonomy rows
-- already carry the transitivity the design asks for. Only the is_hierarchy_rel wiring — the
-- part that is read — remains, and with it the file applies and the ledger stops re-running it.

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format($upd$
            UPDATE %I.rel_types
               SET is_hierarchy_rel = true
             WHERE rel_type IN ('instance_of', 'is_a', 'member_of', 'part_of', 'subclass_of')
        $upd$, _schema);

        RAISE NOTICE 'Migration 089: hierarchy flags wired in %', _schema;
    END LOOP;
END $$;
