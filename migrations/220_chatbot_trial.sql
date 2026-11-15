-- 220_chatbot_trial.sql — the CHATBOT-INTEGRATION TRIAL (SPEC §12) — CLOSED-SOURCE SaaS layer.
--
-- A SEPARATE trial entitlement for the chat-agent feature, DISTINCT from the FaultLine account
-- trial (migration 178 `trial_days` = the first-month-free INDIVIDUAL trial; that clock lives on
-- individual_users.trial_ends_at and auto-purges post-trial). This is the chatbot trial: while
-- active it flips the chat-agent serve entitlement ON for the account up to the 1-seat bucket,
-- REGARDLESS of paid status; on expiry it FAIL-CLOSES (the /v1/agent/chat gate refuses serve and
-- the surface shows the "Add agent" upgrade CTA from Phase 3B). A user may hold BOTH trials, each
-- expiring on its own clock.
--
-- Storage is on the account row (mirrors the agent_feature_* consent-audit pattern from
-- migration 218). Four columns:
--
--   chatbot_trial_started_at   TIMESTAMPTZ NULL — the mint timestamp (when the trial began).
--   chatbot_trial_expires_at   TIMESTAMPTZ NULL — the expiry; the gate treats
--                              expires_at > NOW() (and not revoked) as ACTIVE. NULL = no trial.
--   chatbot_trial_granted_by   TEXT NULL        — the operator who granted/extended (consent audit).
--   chatbot_trial_revoked_at   TIMESTAMPTZ NULL — set when an operator REVOKES the trial early; a
--                              non-NULL value makes the trial inert even before expires_at.
--
-- The trial LENGTH (default 14 days) + a short informational SUNSET (default 24h) are
-- operator-configurable on the platform_settings singleton — but read with a DEDICATED query at
-- grant/read time (not the cached _COLS tuple), so this migration does not touch the fragile
-- positional singleton reader in saas/platform_settings.py. The sunset is NOT a serve grace: the
-- gate fail-closes hard at expires_at. The sunset only lets the operator/feature-state read report
-- "just expired, show the CTA" vs "no trial" for a configurable window.
--
-- The ledger rows for grant/extend/revoke live in saas/entitlements.py
-- (record_chatbot_trial_change, actions agent_trial / agent_trial_extend / agent_trial_off). They
-- are NON-billable (a trial is free) but operator-attributed + audited, mirroring the
-- agent_addon_off pattern.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path — additive; does not weaken per-tenant isolation. Idempotent (ADD COLUMN IF NOT
-- EXISTS); safe to re-run.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS chatbot_trial_started_at TIMESTAMPTZ NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS chatbot_trial_expires_at TIMESTAMPTZ NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS chatbot_trial_granted_by TEXT NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS chatbot_trial_revoked_at TIMESTAMPTZ NULL;

-- Partial index: only rows with an active-ish trial window are interesting for the (future)
-- expiry reaper / operator oversight. Kept narrow so it stays cheap.
CREATE INDEX IF NOT EXISTS idx_accounts_chatbot_trial_expires_at
    ON public.accounts (chatbot_trial_expires_at)
    WHERE chatbot_trial_expires_at IS NOT NULL;

-- Operator-configurable trial LENGTH (days) + informational SUNSET (hours) on the singleton.
-- Defaults: 14-day trial, 24h sunset. The app clamps reads to a sane range and fails safe (14/24)
-- on an unreadable row.
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS chatbot_trial_days INTEGER NOT NULL DEFAULT 14;
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS chatbot_trial_sunset_hours INTEGER NOT NULL DEFAULT 24;

COMMENT ON COLUMN public.platform_settings.chatbot_trial_days IS
    'Operator-configurable CHATBOT-INTEGRATION trial length in DAYS (default 14; SPEC §12). '
    'Distinct from trial_days (the individual first-month-free trial). Read at grant time by '
    'saas/accounts.py::set_chatbot_trial; the serve gate reads the stored expires_at, NOT this.';
COMMENT ON COLUMN public.platform_settings.chatbot_trial_sunset_hours IS
    'Operator-configurable informational SUNSET (hours) after a chatbot trial expires, during '
    'which the feature-state read reports "just expired" so the UI shows the upgrade CTA. NOT a '
    'serve grace — the gate fail-closes hard at expires_at. Default 24.';
