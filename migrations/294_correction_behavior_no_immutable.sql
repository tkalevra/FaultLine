-- Migration 290: correction_behavior 'immutable' → 'supersede' (issue #224, owner ruling 2026-10-07)
-- Date: 2026-10-07
--
-- WHY
-- ---
-- Owner ruling (2026-10-07, live): "everything should be correctable though, there shouldn't be
-- shit marked immutable … everything should be able to be overwritten, user.is.truth." The
-- immutable correction-lane rejections are DELETED in code (main.py correct_fact Phase 6, the
-- correction-extraction prompt, gate.py _apply_correction_semantics). This migration flips the
-- METADATA so no rel_types row keeps asserting a behavior the engine no longer implements —
-- 'immutable' becomes inert by construction (no reader branches on it) and the row no longer lies
-- on the /ontology/rel_types read surface either.
--
-- The seeded 'immutable' rows (migration 007 family): born_in, born_on, parent_of, child_of,
-- sibling_of, instance_of, subclass_of, same_as. All become 'supersede'.
--
-- Idempotent: a second run matches zero rows. Tenant fan-out guarded on table existence.

-- ============================================================================
-- Part 1: public template (the seed source every new tenant copies)
-- ============================================================================
UPDATE public.rel_types
   SET correction_behavior = 'supersede'
 WHERE correction_behavior = 'immutable';

-- ============================================================================
-- Part 2: per-user schemas
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        IF to_regclass(format('%I.rel_types', _schema)) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format(
            'UPDATE %I.rel_types SET correction_behavior = ''supersede''
              WHERE correction_behavior = ''immutable''',
            _schema);
    END LOOP;
END $$;
