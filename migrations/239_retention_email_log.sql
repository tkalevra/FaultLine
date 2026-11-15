-- 239_retention_email_log.sql — the EXACTLY-ONCE dedupe arbiter for retention email.
-- CLOSED-SOURCE SaaS layer.
--
-- Purpose
-- ───────
-- The two retention emails (warning + farewell) MUST each fire EXACTLY ONCE per individual —
-- under restart, under replay, under a scheduler that fires the sweep twice in a minute. The
-- arbiter is a Postgres row with a UNIQUE constraint, NOT in-memory state: a process restart
-- cannot lose what lives in the control plane.
--
-- The PRIMARY KEY (related_user_id, email_kind) IS the exactly-once contract. The sender does
--   INSERT ... ON CONFLICT (related_user_id, email_kind) DO NOTHING
-- and treats affected_rows == 0 as "already sent — skip". That INSERT is atomic, so two
-- concurrent sweeps cannot both send: one wins the row, the other gets zero affected rows and
-- falls through. The result is correct under restart, under replay, and under concurrency.
--
-- The two email_kinds:
--   warning   — fires once, two DAYS BEFORE purge-eligibility (trial_ends_at + grace_days - 2d).
--   farewell  — fires once, ON purge-eligibility-day (trial_ends_at + grace_days).
--
-- This table is WRITE-ONLY from the lifecycle and READ-ONLY forever after — it is the audit
-- trail of who was emailed what and when. The operator purge lane in saas/email_outbox.py
-- (clear) is deliberately NOT extended here: clearing this row would UNSUPPRESS the send and
-- re-fire the email on the next sweep, which is exactly the failure mode this table exists to
-- prevent. If a re-send is genuinely wanted, that is an operator action, not a sweep behavior.
--
-- Control-plane public.* ONLY. Idempotent (IF NOT EXISTS); safe to re-run. Written by
-- saas/retention_emails.py::send_warning / send_farewell; read by the operator audit route.

CREATE TABLE IF NOT EXISTS public.retention_email_log (
    related_user_id UUID NOT NULL,
    email_kind      TEXT NOT NULL,    -- 'warning' | 'farewell'
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    transport       TEXT NOT NULL,    -- 'capture' | 'smtp'
    PRIMARY KEY (related_user_id, email_kind),   -- THE exactly-once constraint
    CONSTRAINT retention_email_log_kind_chk
        CHECK (email_kind IN ('warning', 'farewell')),
    CONSTRAINT retention_email_log_transport_chk
        CHECK (transport IN ('capture', 'smtp'))
);

COMMENT ON TABLE public.retention_email_log IS
    'EXACTLY-ONCE dedupe arbiter for the two retention emails (control-plane public.*). The '
    'PRIMARY KEY (related_user_id, email_kind) IS the contract: the sender does '
    'INSERT ... ON CONFLICT DO NOTHING and treats affected_rows == 0 as "already sent — skip". '
    'Survives restart and replay because it is a Postgres row, not in-memory state. The two '
    'kinds are ''warning'' (two days before purge-eligibility) and ''farewell'' (on purge-'
    'eligibility-day). Written by saas/retention_emails.py::send_warning / send_farewell.';
