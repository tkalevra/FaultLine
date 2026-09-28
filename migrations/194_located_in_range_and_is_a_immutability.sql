-- Migration 194: constrain the auto-grown `located_in` range; make `is_a` immutable.
-- Date: 2026-07-28
--
-- WHY
-- ---
-- Two rel_types rows are wrong in ways that defeat machinery already shipped. BOTH are corrections
-- to GROWTH's own output or to a structural mis-declaration — neither overrides a user-stated fact,
-- and neither invents a new rel. (Authority order: user > seed > growth. `located_in` carries
-- source='wikidata', i.e. it was auto-grown from Wikidata, not hand-curated; correcting an
-- auto-grown range is repairing growth, not overriding curation.)
--
-- (1) located_in.tail_types = {ANY}  ->  {Location}
--     Its own sibling `lives_in` (also source='wikidata') is correctly declared {Location}; the
--     range on `located_in` was simply left unconstrained. The ANY WILDCARD means the SHACL-style
--     value-type check (WGM_SEED_TYPE_CONSTRAINT_ENFORCE, shipped) has NOTHING to enforce, so an
--     ORGANIZATION lands as a place: recall emitted `you are located in Airbnb` (the BRAND, not a
--     lodging) alongside legitimate rows (chicago, las vegas, maui, yellowstone national park).
--     Filing a company as a Location poisons L4 — the place-index the deterministic walk uses.
--
--     SCOPE NOTE (the distinction that matters, and why the fix is a RANGE and not a new rel):
--     "I'm staying at an Airbnb in Chicago" is a TEMPORARY STAY; "I live at X" is RESIDENCE. Those
--     are different relations, and `lives_in`/`lives_at` already own residence. Narrowing this range
--     does NOT collapse the two — it only stops a NON-Location (the Airbnb brand/service) from being
--     filed as a place. The genuine locative in that sentence (chicago) is a Location and still
--     lands. A dedicated temporary-stay rel is deliberately NOT seeded here: minting novel rels is
--     the growth engine's job (frequency-gated via ontology_evaluations), and hand-seeding one would
--     be exactly the hardcoding this codebase forbids.
--
--     WE DON'T FORGET: a now-out-of-range edge is not deleted. The shipped gate demotes a seed
--     type-constraint violation into the EXISTING Class-C quarantine (staged, C->B promotable), so
--     the aspect is retained and can be re-classified later.
--
-- (2) is_a.correction_behavior = 'supersede' -> 'immutable'
--     `is_a` is a CLASSIFICATION relation — set membership, the same family as instance_of and
--     subclass_of, both of which are already declared 'immutable'. Class membership is a STATIC
--     property, not a FLUENT one: static properties are those "whose values do not change in time"
--     vs dynamic (fluent) properties "whose values may change in time" (Batsakis, Petrakis et al.,
--     "Temporal representation and reasoning in OWL 2", Semantic Web 8(6), 2017; 4D-fluents per
--     Welty & Fikes, FOIS 2006). rdf:type is class MEMBERSHIP and rdfs:subClassOf the SUBSET
--     relation (RDF Schema 1.1) — neither has temporal extent.
--
--     CONSEQUENCE, STATED PLAINLY: correction_behavior drives TWO things — (a) the render predicate
--     `_is_static_property` (RENDER_STATIC_PROPERTY_TENSELESS, shipped), so `is_a` stops rendering
--     "X used to be a Y"; and (b) whether a NEW is_a archives the prior one. Under 'immutable' a
--     later is_a no longer auto-supersedes the earlier — competing classifications COEXIST, which
--     matches how instance_of/subclass_of already behave and is the correct reading for a hierarchy
--     rel (a thing can be an instance of several types). This is the intended change, not a
--     side-effect.
--
--     RISK: low and measured — `is_a` had ZERO rows in the live test tenant at the time of writing,
--     so no existing user data changes meaning.
--
-- WHAT
-- ----
-- Updates the two rows in `public.rel_types` (the TEMPLATE new tenants are seeded FROM) and fans the
-- same correction out to every existing `faultline_%` tenant schema.
--
-- IDEMPOTENT: pure UPDATEs to a fixed target value; re-running is a no-op.
-- REVERSIBLE: see the rollback block at the foot of this file.
--
-- ⚠️ OPERATIONAL: this fans out to ALL faultline_* schemas INCLUDING a bench seat. Per
-- the internal design record G9, never run a fan-out migration while a benchmark is live — apply
-- it at deploy time, then restart the bench so the run measures one consistent engine.

BEGIN;

-- (1) + (2) in the public TEMPLATE
UPDATE public.rel_types
   SET tail_types = ARRAY['Location']
 WHERE rel_type = 'located_in'
   AND tail_types @> ARRAY['ANY'];

UPDATE public.rel_types
   SET correction_behavior = 'immutable'
 WHERE rel_type = 'is_a'
   AND correction_behavior IS DISTINCT FROM 'immutable';

-- fan out to every existing tenant
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format(
            'UPDATE %I.rel_types SET tail_types = ARRAY[''Location'']
              WHERE rel_type = ''located_in'' AND tail_types @> ARRAY[''ANY'']',
            _schema);
        EXECUTE format(
            'UPDATE %I.rel_types SET correction_behavior = ''immutable''
              WHERE rel_type = ''is_a'' AND correction_behavior IS DISTINCT FROM ''immutable''',
            _schema);
        RAISE NOTICE 'Migration 194: located_in range + is_a immutability corrected in %', _schema;
    END LOOP;
END $$;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- ROLLBACK (manual; restores the pre-migration declarations)
--
-- UPDATE public.rel_types SET tail_types = ARRAY['ANY']
--  WHERE rel_type = 'located_in';
-- UPDATE public.rel_types SET correction_behavior = 'supersede'
--  WHERE rel_type = 'is_a';
-- DO $$ DECLARE _schema TEXT; BEGIN
--   FOR _schema IN SELECT schema_name FROM information_schema.schemata
--                   WHERE schema_name LIKE 'faultline_%' LOOP
--     EXECUTE format('UPDATE %I.rel_types SET tail_types = ARRAY[''ANY'']
--                      WHERE rel_type = ''located_in''', _schema);
--     EXECUTE format('UPDATE %I.rel_types SET correction_behavior = ''supersede''
--                      WHERE rel_type = ''is_a''', _schema);
--   END LOOP; END $$;
-- ─────────────────────────────────────────────────────────────────────────────
