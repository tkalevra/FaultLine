-- 222_chatbot_trial_days_15.sql — bump the chatbot trial to 15 days (SPEC §13) — CLOSED-SOURCE SaaS.
--
-- SPEC §13 (owner, 2026-08-07): the chatbot trial is 15 DAYS (supersedes the earlier 14-day figure).
-- The code default in saas/accounts._DEFAULT_CHATBOT_TRIAL_DAYS is now 15 — but the OPERATOR-facing
-- length is read from public.platform_settings.chatbot_trial_days, which migration 220 added as
-- INTEGER NOT NULL DEFAULT 14. So the singleton row inherited 14 and chatbot_trial_days() returns
-- 14, overriding the code default. To make the trial ACTUALLY 15 days this migration:
--   1. raises the COLUMN DEFAULT to 15 (fresh installs / a cleared row get 15);
--   2. bumps the existing stored value from 14 → 15 — but ONLY where it is still 14 (the inherited
--      old default). An operator who explicitly tuned it to another value keeps their setting.
--
-- This is the operative half of the 14→15 change; the fail-close-on-expiry / no-auto-subscribe
-- behaviour is unchanged (agent_entitlement.serve_allowed fail-closes on expiry; no job flips
-- agent_feature_enabled). Control-plane public.* ONLY. Idempotent; safe to re-run.

ALTER TABLE public.platform_settings
    ALTER COLUMN chatbot_trial_days SET DEFAULT 15;

UPDATE public.platform_settings
    SET chatbot_trial_days = 15, updated_at = NOW()
    WHERE singleton = TRUE AND chatbot_trial_days = 14;

COMMENT ON COLUMN public.platform_settings.chatbot_trial_days IS
    'Operator-configurable CHATBOT-INTEGRATION trial length in DAYS (default 15; SPEC §13). '
    'Fail-SAFE: saas/accounts.chatbot_trial_days clamps to [1,365] and falls back to 15.';
