-- 309_facts_partial_live_unique.sql — fog-family F8 (R2, lane C, 2026-11-07)
--
-- THE WOUND (critic R1 residual F8): a residence re-statement whose only facts row is
-- RETIRED (superseded) hit the natural-key upsert and could only mutate the archived row
-- in place — resurrection (falsifying the row's validity span) or retirement-preserved
-- no-op (no live city row; the render falls back to street located_in fragments). A
-- same-triple FRESH live row was impossible because the FULL unique constraint owned
-- (subject_id, object_id, rel_type) regardless of retirement.
--
-- THE FIX: replace the full unique constraint with a PARTIAL unique index over LIVE rows
-- (WHERE superseded_at IS NULL). The invariant that matters — at most ONE LIVE row per
-- natural key — is preserved exactly; live-read queries are unchanged. A retired twin no
-- longer blocks a fresh live row, so fact_store's F8 route files copy-or-new-row and never
-- resurrects the archived one. Non-residence rels keep the shipped in-place revive contract
-- (the store inserts with the retired row's own id → facts_pkey conflict → the same DO
-- UPDATE CASEs). The replay freeze contract is enforced by the store's pre-probe (a replay
-- landing on any existing row is skipped entirely).
--
-- Every facts upsert site carries the matching arbiter predicate
-- (ON CONFLICT ... WHERE superseded_at IS NULL): fact_store/store.py (ingest commit),
-- api/main.py (correction temporal-rewire), wgm/gate.py (gate writes),
-- re_embedder/embedder.py (both staged-promotion upserts). staged_facts keeps its own
-- full unique key (no superseded_at there).
--
-- Fan-out covers EVERY schema holding a facts table (public + faultline_% + pin/seat
-- schemas), matching the 308 pattern. Idempotent: constraint drop falls back to index
-- drop; CREATE INDEX IF NOT EXISTS. New tenants inherit via provisioning's migration
-- replay, same as 001-308.

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
                  AND c.relname = 'facts'
                  AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        -- The shipped DDL declares it as a table CONSTRAINT; older provisionings may have
        -- it as a bare unique index. Try constraint first, then index, then give up quietly
        -- (idempotent re-run).
        BEGIN
            EXECUTE format('ALTER TABLE %I.facts DROP CONSTRAINT facts_subject_id_object_id_rel_type_key', _sch);
        EXCEPTION
            WHEN undefined_object THEN
                BEGIN
                    EXECUTE format('DROP INDEX IF EXISTS %I.facts_subject_id_object_id_rel_type_key', _sch);
                EXCEPTION
                    WHEN OTHERS THEN NULL;
                END;
            WHEN OTHERS THEN NULL;
        END;
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS facts_subject_object_rel_live_key ON %I.facts (subject_id, object_id, rel_type) WHERE superseded_at IS NULL',
            _sch);
    END LOOP;
END
$mig$;
