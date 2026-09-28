-- Migration 192: entities.node_role — the FIRST-CLASS value/place property (THE HARD LINE, stamped).
-- Date: 2026-07-20
--
-- WHY
-- ---
-- THE HARD LINE (the truth-firewall): a MEMORY — a specific user VALUE (`blue` in
-- favorite_colour=blue, an address literal, `Rex` a pet name) — must NEVER be treated as an L4 PLACE
-- (a type node reachable by subclass_of/instance_of). A memory is FILED AT a place; it never BECOMES
-- one. Growing a type-ladder off a value both violates the firewall AND (in some tenant states)
-- buries/supersedes the user's value at recall — the authority order (user > seed > growth) inverted.
--
-- Today that distinction is re-derived ad-hoc in 30+ scattered chain-local spots (grep -c "HARD LINE"
-- src/extraction/linguistics.py -> 69), plus the WordNet ladder, the seeded-backbone attach, the
-- Stage-2 suppressor, and — critically — NOT AT ALL in the ASYNC re_embedder growth (which has no
-- in-flight edge to re-derive from). When a layer forgets to re-derive it, a user value is
-- laddered/typed/superseded and buried (the ladder-on-value data-loss class).
--
-- This migration adds the PERSISTED CARRIER for the decision so it is derived ONCE at ingest, stamped
-- on the node, and READ by every downstream consumer (src/api/node_role.py). See
-- the internal design record and the internal design record (finding #3).
--
-- WHAT
-- ----
--   entities.node_role TEXT  — domain {value, place, name, both}; NULL = unstamped (legacy / not yet
--   classified). NULL is the SAFE default: a NULL node falls through to the existing structural
--   re-derivation (the flag-OFF path), so this column is ADDITIVE and INERT until the code reads it
--   under VALUE_PLACE_FIRST_CLASS (default OFF).
--
--   `both` is the OWL 2 PUNNING cell (https://www.w3.org/2007/OWL/wiki/Punning) — a surface that is
--   legitimately BOTH a user value and a type; value protection STILL applies. Reached only by the
--   monotonic authority-ordered transition in node_role.py, never by a bare overwrite.
--
-- Grounding: RDF rdf:type (ABox individual = value) vs rdfs:subClassOf (TBox class = place); WordNet
-- instances have no hyponyms (Miller & Hearst, ACL J06-1001).
--
-- SCOPE: this is a per-tenant DATA-plane column (lives in each faultline_<uuid> schema + the public
-- TEMPLATE that new tenants are seeded from). Only faultline_* schemas are fanned out here.
--
-- BACKFILL: none required. NULL = unstamped is correct and safe. For a future ON-by-default cutover,
-- a backfill would derive node_role from committed edges the same way node_role.stamp_edge_object
-- does (object of instance_of/subclass_of or a constrained-typed rel -> place; object of a
-- user_stated unconstrained/scalar rel -> value; subject of also_known_as/pref_name -> name). Left
-- out deliberately: the flag is OFF and the structural re-derivation covers unstamped nodes.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS); ADDITIVE (no existing column/constraint/index touched);
-- REVERSIBLE (ALTER TABLE ... DROP COLUMN IF EXISTS node_role). Safe to re-run.

-- ── 1. public TEMPLATE (source new tenants are provisioned FROM) ────────────────────
ALTER TABLE public.entities
    ADD COLUMN IF NOT EXISTS node_role TEXT;

COMMENT ON COLUMN public.entities.node_role IS
    'FIRST-CLASS value/place property (THE HARD LINE). {value,place,name,both}; NULL=unstamped. '
    'value/name/both = a MEMORY (never laddered/typed/superseded by inferred type); place = an L4 '
    'type node. both = OWL punning. Derived/stamped/read by src/api/node_role.py under '
    'VALUE_PLACE_FIRST_CLASS (default OFF). See the internal design record';

-- ── 2. fan out to every EXISTING per-tenant data schema ─────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.entities ADD COLUMN IF NOT EXISTS node_role TEXT',
            _schema);
        RAISE NOTICE 'Migration 192: entities.node_role added to %', _schema;
    END LOOP;
END $$;
