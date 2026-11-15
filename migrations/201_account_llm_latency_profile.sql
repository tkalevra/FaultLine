-- Migration 201: BYO-brain LATENCY PROFILE + calibration measurement — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-31
--
-- WHY (the correction this whole feature turns on)
-- ------------------------------------------------
-- On 2026-07-31 a tenant's self-hosted brain looked broken: segmentation silently fell back to
-- deterministic-only capture, turn after turn. The obvious reading — "their model is too slow" —
-- WAS WRONG. The model was fine. OUR single global 6.0s `REFRAME` budget was tuned for a fast
-- hosted endpoint and guillotined a local one. The same number that is correct for a hosted API
-- is hostile to a self-hosted deployment.
--
-- These columns replace that one global default with a MEASURED, per-brain choice, and — the
-- part that matters — they record the measurement that made the choice, so the tenant can see
-- WHY. A silent auto-choice would reproduce the original problem in a new place.
-- See DEV/DESIGN-byo-brain-health-and-alerting.md §1.
--
--   latency_profile         'hosted-fast' | 'local' | 'slow' (saas/brain_profile.py PROFILES).
--                           NULL = never calibrated → NO floors are applied → the engine's
--                           budgets are byte-for-byte what they are today. This migration is
--                           therefore a NO-OP for behaviour until a profile is resolved.
--   latency_profile_source  'measured' (the calibration probe chose it) or 'manual' (the tenant
--                           overrode it). Displayed next to the profile — an override must never
--                           masquerade as a measurement.
--   latency_p50_ms          median of the calibration probe's per-call wall times.
--   latency_p95_ms          nearest-rank 95th percentile of the same samples. With the default
--                           sample count (5) this IS the slowest observed call — the console
--                           says so rather than implying more precision than the sample supports.
--   latency_samples         how many probe calls the measurement is based on. Stored so the
--                           console can qualify the percentile honestly.
--   latency_measured_at     when the measurement was taken (an old measurement is stale advice).
--
-- HOW THE PROFILE BINDS (verify the read, not the write): the value is returned by
-- saas/llm_config.resolve_account_llm_config → carried on the per-request ContextVar
-- (saas/llm_context.current_llm_config) → read by src/api/llm_calls._tenant_timeout_floor →
-- applied in LLMTimeouts.get() as max(env-or-default, profile floor). A profile can only ever
-- WIDEN a budget, never narrow one, so a mis-set profile cannot introduce a new timeout.
--
-- SCOPE NOTE (honest): this is per-ACCOUNT because the BYO brain itself is per-account
-- (migration 135). Self-serve individuals are seats under ONE shared "Singles" house account and
-- therefore already share one brain config row; the profile inherits exactly that existing
-- sharing and does not widen it. Per-SEAT health telemetry (migration 200) is deliberately NOT
-- stored here for that reason.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path. Additive; idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.

ALTER TABLE public.account_llm_config
    ADD COLUMN IF NOT EXISTS latency_profile        TEXT,
    ADD COLUMN IF NOT EXISTS latency_profile_source TEXT,
    ADD COLUMN IF NOT EXISTS latency_p50_ms         INTEGER,
    ADD COLUMN IF NOT EXISTS latency_p95_ms         INTEGER,
    ADD COLUMN IF NOT EXISTS latency_samples        INTEGER,
    ADD COLUMN IF NOT EXISTS latency_measured_at    TIMESTAMPTZ;

COMMENT ON COLUMN public.account_llm_config.latency_profile IS
    'BYO-brain latency profile: hosted-fast | local | slow (saas/brain_profile.py). Supplies '
    'per-operation timeout FLOORS applied as max(env-or-default, floor) in LLMTimeouts.get(). '
    'NULL = never calibrated = no floors = today''s budgets unchanged.';

COMMENT ON COLUMN public.account_llm_config.latency_profile_source IS
    '''measured'' (chosen by the calibration probe) or ''manual'' (tenant override). Shown next '
    'to the profile so an override never reads as a measurement.';

COMMENT ON COLUMN public.account_llm_config.latency_p95_ms IS
    'Nearest-rank p95 of the calibration probe samples. At the default sample count (5) this is '
    'the slowest observed call; the console qualifies it rather than implying more precision.';
