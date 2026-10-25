-- Migration 300: growth_work_queue — the DURABLE BREADCRUMB for brain-starved
-- growth work (gauntlet R8, plan DEV/BUGS/plan-100pct-addressability-20261024.md §R8).
-- Date: 2026-10-24
--
-- WHY
-- ---
-- When `_reembedder_llm_lane_open` (src/re_embedder/embedder.py) closes — the tenant's
-- brain is rate-limited, circuit-broken, posture-cooled, or down — the growth loops
-- (ontology enrichment, synonym convergence, what-is classify / ±6 climb, and the
-- Class-C promotion's classify-before-promote half) skip UNRECORDED. The sweep ledger
-- keeps the subsystem DUE but the system forgets WHAT was starved: which subsystem for
-- which tenant, which staged row was left unpromoted. That is the BUG-005 wedge shape —
-- work due but invisible. Measured shape on prod: document-derived staged facts sit
-- unpromoted and unconverged for hours/days because the deferral is silent.
--
-- The fix is the named SOTA pattern (DEV/RESEARCH/recall-uplift-sota-20261024.md:
-- Letta sleep-time compute, LightRAG KV-cache): "ingest now, enrich later" — persist a
-- work item per deferred operation, drain it priority-first when the lane opens.
--
-- WHAT THIS TABLE IS
-- ------------------
--   operation_type  the growth-loop family: 'enrichment' | 'synonym_convergence' |
--                   'whatis_climb' | 'promotion'. (The precise subsystem rides in
--                   target_id — the lane gate defers a WHOLE subsystem run.)
--   target_id       WHAT was deferred: the subsystem name at a lane-gate deferral, or
--                   the staged_facts.id (as text) for a Class-C promotion deferral.
--   priority        1 (highest) .. 9. Drain order: promotion=2 (recall-visible),
--                   enrichment=5 (the column default), synonym_convergence=6,
--                   whatis_climb=7.
--   completed_at    NULL = pending. Stamped on a clean re-attempt; left NULL on
--                   failure (rate-limited retry — one drain attempt per cycle).
--
--   PARTIAL UNIQUE (operation_type, target_id) WHERE completed_at IS NULL: the same
--   target is never re-deferred while a pending item exists, so cycles of outage
--   collapse onto one row per deferred unit (idempotent write path). Implemented as
--   the house NULL-safe expression form (COALESCE(target_id,'') — same shape as
--   uq_artefacts_identity): a plain UNIQUE treats NULLs as distinct, and this way a
--   NULL target cannot dodge the dedup on any PG in the fleet. Completed rows are
--   exempt — a NEW deferral after completion re-arms — and prunable
--   (growth_work_queue.prune_completed; the drain prunes rows completed more than
--   GROWTH_QUEUE_PRUNE_DAYS ago, default 7).
--
-- THE QUEUE IS NOT THE LEDGER. The sweep ledger decides whether work is DUE; this
-- table records which due work the brain could not do, so recovery is ordered and
-- observable. It gates nothing: every growth subsystem still runs ungated every
-- cycle; the ledger's own rules (including the EXCLUDED list) are untouched.
--
-- BOUNDED: at most GROWTH_QUEUE_MAX (default 5000) PENDING rows per tenant — the
-- write path evicts the OLDEST pending rows beyond the cap (env, live-read).
--
-- NO LLM in the queue mechanics — the drain re-attempts each item by running the
-- growth subsystem's own bounded function (src/re_embedder/growth_work_queue.py is
-- pure SQL bookkeeping).
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so the
-- table MUST exist inside every tenant schema. New tenants get it from the template
-- (src/provisioning/templates/user_schema.sql), updated in this same change — "A
-- migration-only change silently skips every new tenant" (the rule migrations 197/273/
-- 298/299 shipped under). Rides the boot-migration ledger (public.schema_migration_
-- status): applied once per schema, never re-swept.
--
-- GUARDED FANOUT: the loop joins information_schema.schemata (a stale registry row
-- naming a dropped schema cannot abort the DO block). This migration creates a NEW
-- table (no base-table dependency to guard), additive + idempotent only (CREATE TABLE
-- / CREATE INDEX IF NOT EXISTS): no DROP, no UPDATE, no DELETE, no backfill. Safe to
-- re-run. (The cortex schemas are `flagent_<uuid>` and do not match the faultline_%
-- glob — the truth firewall is untouched.)

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.growth_work_queue ('
            '    id             BIGSERIAL   PRIMARY KEY,'
            '    operation_type TEXT        NOT NULL,'
            '    target_id      TEXT,'
            '    priority       INTEGER     DEFAULT 5,'
            '    created_at     TIMESTAMPTZ DEFAULT now(),'
            '    completed_at   TIMESTAMPTZ'
            ')',
            _schema);

        -- The hammering guard: one PENDING row per (operation_type, target_id).
        -- NULL-safe COALESCE expression form — the template's house style.
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_growth_work_pending '
            'ON %I.growth_work_queue (operation_type, COALESCE(target_id, '''')) '
            'WHERE completed_at IS NULL',
            _schema);

        -- The drain's access path: pending rows, priority-first, oldest first.
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_growth_work_drain '
            'ON %I.growth_work_queue (priority, created_at) '
            'WHERE completed_at IS NULL',
            _schema);

        EXECUTE format(
            'COMMENT ON TABLE %I.growth_work_queue IS %L',
            _schema,
            'Migration 300 (gauntlet R8): durable breadcrumb for brain-starved growth '
            'work — what the sweep ledger says is DUE but the tenant brain could not '
            'do (lane closed: rate-limited/circuit-broken/down). operation_type in '
            '(enrichment, synonym_convergence, whatis_climb, promotion); target_id = '
            'the deferred subsystem name or the staged_facts.id. Partial unique keeps '
            'one PENDING row per target; completed rows are prunable; pending rows '
            'bounded by GROWTH_QUEUE_MAX (oldest evicted). NOT a replacement for the '
            'sweep ledger and gates nothing — recovery is drain-on-lane-open, '
            'priority-first, via growth_work_queue.drain.');
    END LOOP;
END $$;
