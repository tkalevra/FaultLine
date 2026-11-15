-- 221_chatbot_trial_reminder.sql — chatbot-trial reminder dedupe (SPEC §13) — CLOSED-SOURCE SaaS.
--
-- The trial-expiry/reminder email sweep (saas/trial_scheduler.run_trial_reminders) sends ONE
-- pre-expiry reminder and ONE post-expiry notice per trial window. To stay idempotent under a
-- repeating daily trigger (and never spam a tenant), the sweep stamps these two columns when it
-- has sent, and skips an account whose stamp already covers the current expiry window.
--
--   chatbot_trial_reminder_sent_at  TIMESTAMPTZ NULL — when the pre-expiry REMINDER was last sent.
--   chatbot_trial_expired_sent_at   TIMESTAMPTZ NULL — when the post-expiry NOTICE was last sent.
--
-- Both are reset to NULL whenever a trial is (re)granted/extended (saas/accounts.set_chatbot_trial),
-- so a fresh window gets a fresh pair of notices. They are advisory dedupe stamps only — the
-- authoritative trial liveness remains chatbot_trial_expires_at / chatbot_trial_revoked_at (220),
-- and the fail-close serve gate never consults them.
--
-- Control-plane public.* ONLY. Additive; idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.
-- Does not weaken per-tenant isolation.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS chatbot_trial_reminder_sent_at TIMESTAMPTZ NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS chatbot_trial_expired_sent_at TIMESTAMPTZ NULL;
