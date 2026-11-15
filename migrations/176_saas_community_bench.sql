-- Migration 176: COMMUNITY self-serve benchmark + public leaderboard — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-14
-- Purpose: back the self-serve "Run your own benchmark" feature (individual /me realm) + the
--          PUBLIC anonymized leaderboard. A signed-in user runs the CLAIMS suite in an EPHEMERAL
--          sandbox against THEIR configured brain, then may PUBLISH an anonymized result row here.
--          The leaderboard page reads ONLY the anonymized columns (model_label, scores, wall_time,
--          created_at). The owning account/user is stored for internal dedupe + rate-limit ONLY and
--          is NEVER served to the public endpoint.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path. Additive; idempotent (IF NOT EXISTS everywhere); safe to re-run. Does NOT touch
-- any per-tenant DATA schema, token, memory, or the search_path rule. No-op semantics when
-- SAAS_MODE is off (the reader/writer modules are only mounted under SAAS_MODE).
--
-- Read/written with: saas/community_bench.py (result publish + anonymized leaderboard read),
-- saas/bench_selftest.py (the sandbox run), saas/bench_report.py (admin report config + cadence).

-- ── (1) Community benchmark results ─────────────────────────────────────────────────────
--   PUBLIC columns  (served to the anonymized leaderboard): model_label, scores, wall_time_ms,
--                    created_at, claims_passed, claims_total.
--   INTERNAL columns (NEVER served publicly): account_id, user_id — used ONLY for per-user
--                    rate-limiting + dedupe. The public read path selects the anon columns only.
--   scores          — jsonb: {"claims_passed": P, "claims_total": T, "by_category": {cat: {passed,total}}}
CREATE TABLE IF NOT EXISTS public.community_bench_results (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- INTERNAL / never public. Nullable so a purged account's row can survive anonymized.
    account_id    UUID,
    user_id       UUID,
    -- PUBLIC / anonymized surface.
    model_label   TEXT NOT NULL,                        -- the brain the user ran (e.g. "qwen3.5-9b")
    scores        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {claims_passed, claims_total, by_category}
    claims_passed INTEGER NOT NULL DEFAULT 0,           -- denormalized for cheap sort/index
    claims_total  INTEGER NOT NULL DEFAULT 0,
    wall_time_ms  BIGINT NOT NULL DEFAULT 0,
    published     BOOLEAN NOT NULL DEFAULT true,         -- soft-hide without deleting history
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT community_bench_counts_chk CHECK (claims_passed >= 0 AND claims_total >= 0)
);
-- Leaderboard read: newest published rows first.
CREATE INDEX IF NOT EXISTS idx_community_bench_published_created
    ON public.community_bench_results (published, created_at DESC);
-- Per-user rate-limit / dedupe lookups (INTERNAL only).
CREATE INDEX IF NOT EXISTS idx_community_bench_user_created
    ON public.community_bench_results (user_id, created_at DESC);

COMMENT ON TABLE public.community_bench_results IS
    'Community self-serve benchmark results (control-plane public.*). PUBLIC anonymized columns '
    '(model_label, scores, claims_passed/total, wall_time_ms, created_at) drive the public '
    'leaderboard; account_id/user_id are INTERNAL (rate-limit + dedupe) and NEVER served publicly. '
    'One row per PUBLISHED run; sandbox runs that are not shared are never inserted.';
COMMENT ON COLUMN public.community_bench_results.account_id IS
    'INTERNAL owner (rate-limit/dedupe) — NEVER exposed on the public leaderboard endpoint.';
COMMENT ON COLUMN public.community_bench_results.user_id IS
    'INTERNAL owner (rate-limit/dedupe) — NEVER exposed on the public leaderboard endpoint.';

-- ── (2) Admin report configuration ──────────────────────────────────────────────────────
-- A SINGLE control-plane config row (operator-managed): the recipient list + the "every X
-- entries" cadence + the double-send guard (last_report_at / last_reported_count). The
-- one-time "send me a report now" is an ACTION (operator route), not stored here.
CREATE TABLE IF NOT EXISTS public.bench_report_config (
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    recipients          TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],  -- administrator email addresses
    cadence_entries     INTEGER NOT NULL DEFAULT 25,              -- email every X new published results
    last_report_at      TIMESTAMPTZ,
    last_reported_count INTEGER NOT NULL DEFAULT 0,               -- result count at the last auto-send
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT bench_report_singleton_chk CHECK (id = 1),
    CONSTRAINT bench_report_cadence_chk CHECK (cadence_entries >= 1)
);
-- Seed the singleton row so readers never hit an empty table (idempotent).
INSERT INTO public.bench_report_config (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE public.bench_report_config IS
    'Operator config for the community-benchmark email report (control-plane public.*). Singleton '
    'row: recipients[] + cadence_entries (email every X new results) + last_report_at / '
    'last_reported_count (double-send guard). The one-time "send me now" is an action, not stored.';
