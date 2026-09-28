-- Migration 207: public.sweep_work_state — per-SEAT, per-subsystem work ledger
-- Date: 2026-08-01
--
-- WHY
-- ---
-- The re_embedder's per-tenant sweep fires every REEMBED_INTERVAL and re-does identical work
-- for EVERY ready seat regardless of whether that seat did anything. Measured on live boxes
-- 2026-08-01:
--   153 LLM calls started / 152 succeeded in 20 minutes (~8/min, sustained, forever), and on
--   a box whose LLM endpoint was unreachable the sweep retried it every cycle, feeding the
--   circuit breaker.
-- The cost is O(seats) per interval FOREVER while the useful work is O(seats that changed).
-- It scales with growth; the value does not.
--
-- WHAT THIS TABLE IS
-- ------------------
-- A change-token ledger, one row per (seat, subsystem):
--   work_token      — bumped by the WRITER whose write creates work for that subsystem.
--   last_run_token  — the token value the sweep OBSERVED when it last completed that subsystem.
--   last_run_at     — when that completion happened.
-- The sweep runs a subsystem when: no row yet (never run) OR work_token <> last_run_token
-- (dirty) OR last_run_at is older than the max-run interval (the time bound). Otherwise it
-- skips, having issued ZERO queries against the tenant schema and ZERO LLM calls.
--
-- This is a DIRTY FLAG, deliberately NOT a scan-and-compare fingerprint. Deriving "did the
-- input change?" by scanning each subsystem's input rows every cycle is itself per-cycle work
-- that scales with data volume — the same shape of cost, moved rather than removed. The writer
-- already knows it created work; it says so, once, for free.
--
-- WHY `public` AND NOT THE PER-TENANT SCHEMA (deliberate, justified)
-- -----------------------------------------------------------------
-- The standing rule is "public.* is a SEED TEMPLATE, never read at runtime". That rule exists
-- to stop one tenant's ONTOLOGY GROWTH polluting the seed or another tenant. It is a rule about
-- KNOWLEDGE, and this table holds none: two integers and two timestamps per seat, no user
-- content, no ontology, nothing another tenant could ever read as memory.
--
-- The rule already has a runtime exception of exactly this kind, in exactly this loop:
-- `public.user_provisioning` is SELECTed at the top of EVERY sweep cycle
-- (src/re_embedder/embedder.py, the `ready_schemas` query) to decide which tenants exist.
-- Control-plane tables keyed by seat live in public; knowledge does not.
--
-- And per-tenant placement would DEFEAT THE PURPOSE. The whole point is to decide whether to
-- open a per-tenant connection AT ALL. Reading a per-tenant table requires opening that
-- connection first — O(seats) connections and O(seats) search_path binds per cycle, which is
-- the cost being eliminated. One public table answers for every seat in ONE query.
--
-- ISOLATION: every read and every write is keyed by user_id; no code path in this feature ever
-- reads a row it did not key. There is nothing here to leak.
--
-- NOT ADDED TO src/provisioning/templates/user_schema.sql, on purpose: that template creates
-- PER-TENANT objects, and this table is intentionally not one. Adding it there would create 5k
-- copies of a table whose entire value is being singular.
--
-- IDEMPOTENT: safe to re-run; every statement is IF NOT EXISTS / guarded.

CREATE TABLE IF NOT EXISTS public.sweep_work_state (
    user_id         UUID        NOT NULL,
    -- Subsystem name, matching src/re_embedder/sweep_ledger.SUBSYSTEMS. Free text rather than
    -- an enum: adding a subsystem must not need a migration, and an unknown name here is
    -- harmless (the ledger fails toward RUN for anything it does not recognise).
    subsystem       TEXT        NOT NULL,
    -- Monotonic change token. Bumped by writers; never reset. BIGINT so it cannot realistically
    -- wrap, and the comparison is equality (not ordering), so even a wrap would be safe.
    work_token      BIGINT      NOT NULL DEFAULT 1,
    -- The token the sweep OBSERVED at claim time, written back only after the subsystem
    -- COMPLETED. NULL = never completed → always due. Recording the OBSERVED token (not the
    -- current one) is what makes a write that lands DURING a run survive: that write bumps
    -- work_token past the observed value, so the row is dirty again the moment the run ends.
    last_run_token  BIGINT,
    last_run_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, subsystem)
);

COMMENT ON TABLE public.sweep_work_state IS
    'Per-seat, per-subsystem re_embedder work ledger (change token + last-run). CONTROL PLANE, '
    'not knowledge: no user content, no ontology. Writers bump work_token when they create work; '
    'the sweep skips a subsystem whose work_token equals its last_run_token and whose last_run_at '
    'is within the max-run interval. Placed in public deliberately — see migration 207.';

COMMENT ON COLUMN public.sweep_work_state.work_token IS
    'Bumped by the WRITER that created work for this subsystem. A missed writer means this seat '
    'sweeps LATE (bounded by REEMBED_SWEEP_MAX_INTERVAL), never NEVER.';

COMMENT ON COLUMN public.sweep_work_state.last_run_token IS
    'work_token as OBSERVED at claim time, written back only on a completed run. NULL = never '
    'completed. A write landing mid-run bumps work_token beyond this, so it is not swallowed.';

-- The sweep''s one query per cycle is "give me every row for these seats". The PK already
-- serves it. This partial index serves the narrower "which seats are dirty right now?" probe
-- used by the ops/audit lever and by monitoring, without scanning clean rows.
CREATE INDEX IF NOT EXISTS idx_sweep_work_state_dirty
    ON public.sweep_work_state (user_id)
    WHERE last_run_token IS NULL OR last_run_token <> work_token;

-- Housekeeping: a deprovisioned seat leaves rows behind. Cheap to clean, and doing it here
-- keeps the table proportional to LIVE seats rather than to every seat that ever existed.
DELETE FROM public.sweep_work_state s
 WHERE NOT EXISTS (
     SELECT 1 FROM public.user_provisioning up WHERE up.user_id = s.user_id
 );
