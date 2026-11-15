-- Migration 143: SaaS per-seat usage metering — flow counters + memory gauge + plan caps
-- (CLOSED-SOURCE SaaS layer, Phase 9). Date: 2026-07-06
--
-- Implements DEV/DESIGN-saas-pricing-and-metering.md §4: three control-plane tables that make
-- per-seat usage transparent AND enforce the two cost-driving HARD caps. Everything here is a
-- NO-OP when SAAS_MODE is off (the tables simply sit empty / unread) — flag-off = today's
-- behavior EXACTLY.
--
--   * usage_counters — monthly FLOW meters, per-seat, one UPSERT per metered call.
--   * usage_gauges   — the memories_kept STOCK, sampled by the re_embedder (not summed).
--   * plan_limits    — tier -> per-metric cap + hardness, keyed by SEAT TYPE (migration 139
--                      moved the tier onto the seat), NOT the doc's account-level plan.
--
-- Caps are keyed by seat_type {standard, advanced, expert, agent} (the migration-139 ladder),
-- NOT the pricing doc's mid/power/base/agent. The memories_kept ladder is the operator-confirmed
-- 25k/100k/250k/1M; the other four meters ladder monotonically from the doc's tier columns
-- (§3.1) so each tier is strictly >= the one below it:
--
--   metric          | standard | advanced | expert  | agent      | hardness
--   ----------------|----------|----------|---------|------------|---------
--   memories_kept   |   25,000 |  100,000 | 250,000 |  1,000,000 | HARD   (drives re_embedder cost)
--   topics_trained  |        3 |       15 |      30 |         50 | HARD   (priciest op + L4 sprawl)
--   conversations   |    5,000 |   25,000 |  60,000 |    250,000 | soft
--   documents       |       50 |      300 |     750 |      5,000 | soft
--   questions       |   25,000 |  150,000 | 400,000 |         -1 | soft   (-1 = unlimited, agent-serve)
--
-- memories_kept is a per-seat STOCK (no period); every other meter is a monthly FLOW keyed by
-- period='YYYY-MM' (UTC) so a new month starts fresh rows at 0 via the UPSERT — no reset job.
--
-- Control-plane public.* (read at request time). NEVER seeded into a tenant schema, NEVER read
-- from a tenant search_path. Idempotent + forward-only. Read with:
--   DEV/DESIGN-saas-pricing-and-metering.md, DEV/pricing-model.md, saas/metering.py, saas/eventlog.py

-- ── FLOW counters: monthly, per-seat, per-metric ────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.usage_counters (
    account_id  UUID   NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    user_id     UUID   NOT NULL,                    -- the seat (per-UUID transparency)
    period      TEXT   NOT NULL,                    -- billing window 'YYYY-MM' (UTC)
    metric      TEXT   NOT NULL,                    -- 'conversations'|'documents'|'questions'|'topics_trained'
    count       BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, user_id, period, metric)
);
CREATE INDEX IF NOT EXISTS idx_usage_counters_acct_period
    ON public.usage_counters (account_id, period);

-- ── STOCK gauge: current stored-memory count per seat (sampled, not summed) ──────────
CREATE TABLE IF NOT EXISTS public.usage_gauges (
    account_id   UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    user_id      UUID NOT NULL,
    metric       TEXT NOT NULL,                     -- 'memories_kept'
    value        BIGINT NOT NULL DEFAULT 0,
    sampled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, user_id, metric)
);

-- ── PLAN cap map: seat_type -> per-metric limit + hardness ──────────────────────────
CREATE TABLE IF NOT EXISTS public.plan_limits (
    plan       TEXT   NOT NULL,                     -- seat_type: 'standard'|'advanced'|'expert'|'agent'
    metric     TEXT   NOT NULL,
    limit_val  BIGINT NOT NULL,                     -- -1 = unlimited
    enforce    TEXT   NOT NULL DEFAULT 'soft',      -- 'soft' (warn+overage) | 'hard' (block)
    per_seat   BOOLEAN NOT NULL DEFAULT TRUE,       -- limit is per-seat vs per-account
    PRIMARY KEY (plan, metric)
);

-- Seed the laddered caps. Idempotent: ON CONFLICT refreshes limit/hardness so a re-run (or a
-- future cap tune landed here) re-asserts the canonical values without a manual wipe.
INSERT INTO public.plan_limits (plan, metric, limit_val, enforce, per_seat) VALUES
    -- memories_kept — HARD (the one meter tied to the dominant re_embedder cost)
    ('standard', 'memories_kept',   25000,  'hard', TRUE),
    ('advanced', 'memories_kept',  100000,  'hard', TRUE),
    ('expert',   'memories_kept',  250000,  'hard', TRUE),
    ('agent',    'memories_kept', 1000000,  'hard', TRUE),
    -- topics_trained — HARD (priciest op + L4 sprawl guard), monthly
    ('standard', 'topics_trained',      3,  'hard', TRUE),
    ('advanced', 'topics_trained',     15,  'hard', TRUE),
    ('expert',   'topics_trained',     30,  'hard', TRUE),
    ('agent',    'topics_trained',     50,  'hard', TRUE),
    -- conversations — soft, monthly
    ('standard', 'conversations',    5000,  'soft', TRUE),
    ('advanced', 'conversations',   25000,  'soft', TRUE),
    ('expert',   'conversations',   60000,  'soft', TRUE),
    ('agent',    'conversations',  250000,  'soft', TRUE),
    -- documents — soft, monthly
    ('standard', 'documents',          50,  'soft', TRUE),
    ('advanced', 'documents',         300,  'soft', TRUE),
    ('expert',   'documents',         750,  'soft', TRUE),
    ('agent',    'documents',        5000,  'soft', TRUE),
    -- questions — soft, monthly (-1 = unlimited on agent during serve)
    ('standard', 'questions',       25000,  'soft', TRUE),
    ('advanced', 'questions',      150000,  'soft', TRUE),
    ('expert',   'questions',      400000,  'soft', TRUE),
    ('agent',    'questions',          -1,  'soft', TRUE)
ON CONFLICT (plan, metric)
DO UPDATE SET limit_val = EXCLUDED.limit_val,
              enforce   = EXCLUDED.enforce,
              per_seat  = EXCLUDED.per_seat;
