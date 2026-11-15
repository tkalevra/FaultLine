-- 241_email_feature_flags.sql — runtime-flippable feature flags for the email lifecycle.
-- CLOSED-SOURCE SaaS layer.
--
-- Purpose
-- ───────
-- The EMAIL LIFECYCLE feature (warning + farewell emails around individual retention expiry) and
-- the EMAIL KILL SWITCH must be flippable from the operator console WITHOUT a process restart.
-- Env-only flags would require a restart (and the kill switch is the one lever you want live
-- the instant you flip it). So both live as columns on the platform_settings singleton, with the
-- env vars serving as deploy-time overrides only.
--
-- Resolution order in saas/email_controls_routes.py / saas/retention_emails.py:
--   SAAS_EMAIL_KILL_SWITCH env truthy → KILL is ON (wins over the DB column; emergency lever).
--   else → public.platform_settings.email_kill_switch (default FALSE).
--   SAAS_RETENTION_EMAILS_ENABLED env set → that value wins (deploy-time force-on or force-off).
--   else → public.platform_settings.retention_emails_enabled (default FALSE).
--
-- The feature fires ONLY when retention_emails_enabled resolves TRUE. The kill switch suppresses
-- EVERY outgoing email (capture AND live SMTP) when TRUE — it is the master "shut it all off"
-- lever, distinct from the feature flag (which gates only the retention-email sweep).
--
-- Capture-only mode is NOT a flag here — it is DERIVED from the payment provider's mode
-- (saas/payment_config.py): when mode=test, capture is forced; when mode=live, the SMTP relay
-- is used. SAAS_EMAIL_CAPTURE_ONLY env may force it for testing. See saas/smtp_sender.py.
--
-- Control-plane public.* ONLY. Idempotent; safe to re-run. Read via saas/platform_settings.state().
-- Migration number note: 240 was already taken by 240_billing_operator_perms.sql from a parallel
-- workstream; this landed at 241.

-- retention_emails_enabled: the FEATURE FLAG for the retention-email lifecycle. DEFAULT FALSE —
-- the feature ships DARK. The owner promotes it to live alongside the payment provider going
-- LIVE (see the "fires ONLY when (a) the feature flag is enabled AND (b) the payment provider
-- is in LIVE mode" rule in saas/retention_emails.py).
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS retention_emails_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- email_kill_switch: the master "suppress EVERY outgoing email" lever (capture AND live SMTP).
-- DEFAULT FALSE — email flows normally. When TRUE, send_email short-circuits before any capture
-- or SMTP call and returns {killed: True}. Distinct from the feature flag (which gates only the
-- retention-email sweep) — this is the platform-wide shut-off for transactional email too.
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS email_kill_switch BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.platform_settings.retention_emails_enabled IS
    'Feature flag for the retention-email lifecycle (control-plane public.*). DEFAULT FALSE — the '
    'feature ships DARK and is promoted alongside the payment provider going LIVE. When FALSE, '
    'saas/retention_emails.py::sweep is a no-op (no warning, no farewell). Env '
    'SAAS_RETENTION_EMAILS_ENABLED overrides this value when set (deploy-time force).';
COMMENT ON COLUMN public.platform_settings.email_kill_switch IS
    'Master "suppress EVERY outgoing email" lever (control-plane public.*). DEFAULT FALSE — email '
    'flows normally. When TRUE, saas/smtp_sender.py::send_email short-circuits before any capture '
    'or SMTP attempt and returns {killed: True}. Env SAAS_EMAIL_KILL_SWITCH overrides this value '
    'when set (emergency lever).';
