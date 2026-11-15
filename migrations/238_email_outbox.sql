-- 238_email_outbox.sql — the EMAIL OUTBOX capture table (control-plane public.*).
-- CLOSED-SOURCE SaaS layer.
--
-- Purpose
-- ───────
-- When the platform is in TEST mode (or capture-only is forced), EVERY outgoing email is written
-- here INSTEAD of being handed to SMTP. That makes the sandbox pass of the EMAIL LIFECYCLE
-- provable: the copy and the exactly-once-ness can be inspected from the operator console
-- without a single message ever reaching a real recipient. The capture is the sandbox's source
-- of truth; the SMTP relay is for LIVE mode only (saas/smtp_config.py / saas/smtp_sender.py).
--
-- The table is control-plane public.* ONLY. It holds recipient addresses + rendered bodies —
-- the SAME material the live relay would carry — so an operator audit reads identically in
-- either mode. NEVER seeded into a tenant schema (this is platform email, not tenant memory).
--
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run. Written by saas/email_outbox.py from
-- saas/smtp_sender.py::send_email (the single gate every outgoing email flows through).

CREATE TABLE IF NOT EXISTS public.email_outbox (
    id              BIGSERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 'capture' = sandbox capture (never delivered); 'smtp' = a live SMTP send ATTEMPTED.
    -- Capture is the only mode that flows from sandbox; 'smtp' rows are written by future
    -- delivery-receipt paths, not the sandbox capture lane.
    transport       TEXT NOT NULL,
    to_address      TEXT NOT NULL,
    from_address    TEXT,
    subject         TEXT NOT NULL,
    text_body       TEXT NOT NULL,
    html_body       TEXT,
    -- 'retention_warning' | 'retention_farewell' | 'reset' | 'welcome' | 'set_password' |
    -- 'signup_notice' | 'cheeky' | 'final_rejection' | 'trial_started' | 'trial_expiry' |
    -- 'invoice' | 'brain_alert' | 'digest' | 'transactional' (default) | ...
    reason          TEXT NOT NULL,
    -- The individual whose expiry triggered the row (nullable: most transactional email has no
    -- related user). Captured for operator audit; never used for routing.
    related_user_id UUID,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT email_outbox_transport_chk
        CHECK (transport IN ('capture', 'smtp'))
);

-- Operator audit reads: a recent feed + per-reason drilldown + per-user history.
CREATE INDEX IF NOT EXISTS idx_email_outbox_captured_at
    ON public.email_outbox (captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_outbox_reason_captured
    ON public.email_outbox (reason, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_outbox_related_user
    ON public.email_outbox (related_user_id);

COMMENT ON TABLE public.email_outbox IS
    'Sandbox CAPTURE table for outgoing email (control-plane public.*). When the platform is in '
    'TEST mode — or SAAS_EMAIL_CAPTURE_ONLY is set, or SAAS_EMAIL_KILL_SWITCH is NOT set — every '
    'send is written here INSTEAD of going to the SMTP relay, so the email-lifecycle feature can '
    'be proven end-to-end without a single message reaching a real recipient. Inspectable from '
    'the operator console. NEVER seeded into a tenant schema. Written by saas/email_outbox.py from '
    'saas/smtp_sender.py::send_email.';
COMMENT ON COLUMN public.email_outbox.transport IS
    'capture = sandbox capture (never delivered); smtp = a live SMTP send was attempted (written '
    'by future delivery-receipt paths, not the sandbox capture lane).';
COMMENT ON COLUMN public.email_outbox.reason IS
    'The reason/category of this email (retention_warning | retention_farewell | reset | welcome | '
    'set_password | signup_notice | cheeky | final_rejection | trial_started | trial_expiry | '
    'invoice | brain_alert | digest | transactional | ...). Drives operator-console drilldowns.';
