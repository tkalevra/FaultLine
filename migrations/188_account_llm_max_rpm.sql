-- Migration 188: per-account BYO-brain CONFIGURABLE RATE LIMIT (max_rpm) — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-18
-- Purpose: let an account store its OWN outbound LLM rate limit (requests-per-minute) for the
--          engine-brain endpoint, INSTEAD of relying only on the process-global env LLM_MAX_RPM.
--          This is the persisted half of the per-brain "quick-set" rate control: a tenant may set
--          a limit UP TO — never above — the provider's published account-wide maximum for its
--          endpoint. The clamp lives in the application (saas/llm_config.set_account_max_rpm on
--          WRITE, src/api/llm_calls tightest-of-three on READ); this column is the storage.
--
--   max_rpm — the account's configured outbound requests-per-minute ceiling for its engine brain.
--             NULL (the default, and every existing row) = "no per-brain override" → the effective
--             rate stays exactly what it is today: min(global env LLM_MAX_RPM, provider_max*headroom
--             for a KNOWN host). So this migration is a NO-OP for behavior until an account sets it.
--             A stored value is already clamped to the endpoint's provider max on write; the pacing
--             path RE-clamps (min against provider_max*headroom) as defense-in-depth, so it is
--             STRUCTURALLY impossible for a stored value to exceed the provider's published max.
--
-- Read/written with: saas/llm_config.py (_build_config resolve, set_account_max_rpm setter,
-- get_config_redacted, get_account_rate_limit_info), src/api/llm_calls.py (_configured_brain_max_rpm
-- + _effective_rate_per_sec tightest-of-three), saas/tenant_console.py (GET/PUT /tenant/llm-config/rate-limit).
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path — additive; does not weaken per-tenant isolation. Idempotent (ADD COLUMN IF NOT
-- EXISTS); safe to re-run.

ALTER TABLE public.account_llm_config
    ADD COLUMN IF NOT EXISTS max_rpm INTEGER;

COMMENT ON COLUMN public.account_llm_config.max_rpm IS
    'Per-account configured outbound LLM requests-per-minute ceiling for the engine brain. '
    'NULL = no override (effective rate = min(global env LLM_MAX_RPM, provider_max*headroom)). '
    'Clamped to the endpoint provider max on write (saas/llm_config.set_account_max_rpm) and '
    're-clamped at pacing time (src/api/llm_calls tightest-of-three) — structurally cannot '
    'exceed the provider published max on any model.';
