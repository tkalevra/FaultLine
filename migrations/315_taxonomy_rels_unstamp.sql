-- 315_taxonomy_rels_unstamp.sql — the SEC-BUG-3 shelf repair (follows 314).
--
-- WHAT
-- ----
-- One corrective UPDATE in EVERY schema holding rel_types: un-stamp the TAXONOMY /
-- structural lanes that migration 314's category admission caught in its net:
--
--     UPDATE rel_types SET is_sensitive = false
--      WHERE is_sensitive AND is_hierarchy_rel IS TRUE
--
-- WHY
-- ---
-- The live seed carries the taxonomy rels (subclass_of / instance_of / is_a) with
-- category 'identity' — a WIKIDATA-sourced lane meaning CLASSIFICATION, not
-- person-identity data. Migration 314's admission (b) stamped every contact/
-- identity/network-category row sensitive, and the net caught the mechanism's own
-- plumbing: the negative-space serve's is_hierarchy_rel two-hop bridge DRIVES on
-- exactly these rows. Measured on pre-prod seat f34887dd (2026-11-12): the seat's
-- fresh-mint vocabulary census carried subclass_of/instance_of/is_a stamped
-- is_sensitive=true — the cats-class bridge would render no taxonomy content and the
-- aspect-affinity floor (which counts the subclass-of rows' query-token overlap) would
-- starve the cats litmus the mechanism exists to serve. The RUNTIME gate carries the
-- same exemption (the shelf skip in src/mcp/server.py gates `and not hier`), so this
-- file is METADATA TRUTH repair: no future consumer of is_sensitive should read the
-- structural lanes as personal data. Rows the code never gates on hierarchy grounds
-- stay as 314 stamped them (also_known_as / same_as / pref_name / member_of /
-- nationality — the alias and person-identity lanes; withholding them from the
-- broad-serve is the conservative direction and the walk's direct-ask path is
-- untouched).
--
-- IDEMPOTENT: a pure is_sensitive=false stamp over already-false rows. FAN-OUT: every
-- schema holding rel_types, the 308/309/313/314 pattern; the guarded per-schema loop
-- never aborts the whole DO block on a partially-provisioned schema.
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
                  AND c.relname = 'rel_types'
                  AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        BEGIN
            EXECUTE format($ddl$
                UPDATE %I.rel_types
                   SET is_sensitive = false
                 WHERE is_sensitive AND is_hierarchy_rel IS TRUE$ddl$, _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;
            WHEN OTHERS THEN NULL;
        END;
    END LOOP;
END
$mig$;
