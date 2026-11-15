-- Migration 174: PLATFORM offline-mode + SIGNUP-APPROVAL gate — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-14
-- Purpose: two operator-owned, fail-CLOSED gates + the notification surface they need:
--
--   (1) OFFLINE MODE — a global maintenance kill-switch. When ON the PRODUCT is offline for
--       users (data plane + tenant/individual consoles), but the OPERATOR console stays FULLY
--       available — that console is where the switch LIVES, so locking the operator out of it
--       would strand the kill-switch in the ON position with no way back. The operator-realm
--       exemption is therefore load-bearing, not a convenience (see src/mcp/http_server.py
--       _offline_gate + saas/platform_settings.py::offline_state).
--
--   (2) SIGNUP APPROVAL — a new self-serve user is NOT active until a global admin approves.
--       The ONLY self-serve user-creation path is POST /me/signup (saas/user_console.py); every
--       other "user creation" (register_seat, dashboard_users, onboarding tokens) is already a
--       deliberate act by an authenticated admin and is approved-by-construction. The gate
--       therefore sits at /me/signup ONLY — approving an admin's own action would be theatre.
--
-- Both flags live on ONE control-plane public.* SINGLETON (public.platform_settings), read at
-- request time exactly like migration 162's smtp_config. NEVER seeded into a tenant schema and
-- NEVER read from a tenant search_path — additive; does not weaken per-tenant isolation.
--
-- The approval QUEUE (public.signup_approvals) deliberately MIRRORS migration 164's
-- public.reset_approvals shape (pending → approved | denied | expired; queue is authoritative,
-- email only notifies; single-decision enforced by one UPDATE). Same pattern, separate table:
-- the lifecycles differ (an approval here ACTIVATES a login + provisions a schema, it does not
-- mint a reset token), and overloading reset_approvals' CHECK constraints would have made both
-- state machines lie about what they hold.
--
-- Read/written with: saas/platform_settings.py, saas/signup_approvals.py, saas/user_console.py,
-- saas/admin.py, saas/alerts.py.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── (1) PLATFORM_SETTINGS — the operator-owned platform singleton ─────────────────────
CREATE TABLE IF NOT EXISTS public.platform_settings (
    -- Singleton latch: exactly one platform settings row (id is always TRUE).
    singleton               BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),

    -- ── offline / maintenance switch ──
    -- FALSE = normal service. TRUE = the product is offline for users; the operator console
    -- (and ONLY the operator console) remains reachable.
    offline_mode            BOOLEAN NOT NULL DEFAULT FALSE,
    -- Operator-authored copy shown on the maintenance page / returned in the 503 body. NULL =
    -- the built-in default sentence. NEVER interpolated as HTML (escaped at render).
    offline_message         TEXT,
    offline_since           TIMESTAMPTZ,                 -- when the switch was last flipped ON
    offline_by              TEXT,                        -- operator identity that flipped it

    -- ── signup approval gate ──
    -- TRUE (DEFAULT) = a new /me/signup lands 'pending_approval' and cannot log in until a
    -- global admin approves. Fail-closed by default: the safe posture is to hold, not to admit.
    require_signup_approval BOOLEAN NOT NULL DEFAULT TRUE,

    -- ── notification surfaces ──
    -- Arbitrary operator-chosen address notified on a new pending signup. NULL = no notify (the
    -- dashboard QUEUE is still authoritative — email is only the convenience, exactly as in the
    -- reset-approval design). Never client-supplied: written only through the operator realm.
    notify_email            TEXT,
    -- The From: address the WELCOME email is sent as. Defaults to the product no-reply mailbox;
    -- operator-editable (DB-driven, never hardcoded in the sender). Falls back to the SMTP
    -- config's from_address when blank.
    welcome_from_address    TEXT DEFAULT 'noreply@example-host.com',

    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by              TEXT                         -- operator identity that last wrote
);

-- Seed the singleton (idempotent) so the read side always finds a row. Defaults = service ONLINE
-- with signup approval REQUIRED (the fail-closed posture the owner asked for).
INSERT INTO public.platform_settings (singleton)
VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

COMMENT ON TABLE public.platform_settings IS
    'Operator-owned PLATFORM singleton (control-plane public.*): the global offline/maintenance '
    'switch (operator console is ALWAYS exempt — it hosts the switch), the new-user signup-'
    'approval requirement (default ON = fail-closed), the operator notification address, and the '
    'welcome-email From:. Read at request time; never seeded into a tenant schema.';
COMMENT ON COLUMN public.platform_settings.offline_mode IS
    'Global maintenance kill-switch. TRUE = product offline for users (data plane + tenant/'
    'individual consoles 503). The OPERATOR console is NEVER gated by this — it is where the '
    'switch lives. Enforced in src/mcp/http_server.py::_offline_gate (outermost middleware).';
COMMENT ON COLUMN public.platform_settings.require_signup_approval IS
    'TRUE (default) = a new POST /me/signup is held pending a global-admin decision: the login '
    'row is created ''pending_approval'' (cannot authenticate — get_individual_by_* filter '
    'status=''active''), NO seat, NO schema, NO token. Approval activates it.';

-- ── (2) SIGNUP_APPROVALS — the pending-user QUEUE (mirrors public.reset_approvals) ────
-- One row per held signup. The DASHBOARD PENDING QUEUE is authoritative; email only notifies, so
-- the workflow degrades gracefully when SMTP is unconfigured. State: pending → (approved |
-- denied | expired). The realm is ALWAYS 'operator' — a new user is admitted to the PLATFORM,
-- which is the global admin's call, never a tenant's (kept as an explicit column so a future
-- tenant-scoped signup queue plugs in without a schema change).
CREATE TABLE IF NOT EXISTS public.signup_approvals (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The individual_users row being admitted. CASCADE: deleting the login drops its queue item.
    individual_id  UUID NOT NULL REFERENCES public.individual_users(id) ON DELETE CASCADE,
    account_id     UUID,                            -- the backing house account (queue display)
    approver_realm TEXT NOT NULL DEFAULT 'operator',-- 'operator' (global admin approves signups)
    email          TEXT,                            -- denormalized recipient (queue display + notify)
    display_name   TEXT,                            -- what the user called themselves (display)
    status         TEXT NOT NULL DEFAULT 'pending', -- pending | approved | denied | expired
    requested_ip   TEXT,                            -- request source IP (audit / abuse triage)
    decided_by     TEXT,                            -- approver identity (operator principal)
    decided_at     TIMESTAMPTZ,
    expires_at     TIMESTAMPTZ NOT NULL,            -- auto-expire window (see APPROVAL_TTL_DAYS)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT signup_approvals_realm_chk  CHECK (approver_realm IN ('operator', 'tenant')),
    CONSTRAINT signup_approvals_status_chk CHECK (status IN ('pending', 'approved', 'denied', 'expired'))
);
-- Queue reads: pending rows for a realm, newest first.
CREATE INDEX IF NOT EXISTS idx_signup_approvals_realm_status
    ON public.signup_approvals (approver_realm, status);
-- Supersede-siblings + expiry sweeps scope by target.
CREATE INDEX IF NOT EXISTS idx_signup_approvals_target
    ON public.signup_approvals (individual_id, status);

COMMENT ON TABLE public.signup_approvals IS
    'New-user SIGNUP approval queue (control-plane public.*). One row per held /me/signup: '
    'pending → approved|denied|expired, decided by the OPERATOR (global admin). Mirrors '
    'public.reset_approvals (migration 164) deliberately — same state machine, same '
    'queue-is-authoritative/email-only-notifies contract. Written by saas/signup_approvals.py.';

-- ── (3) individual_users.status — admit the new 'pending_approval' value ──────────────
-- No CHECK constraint exists on the column (migration 149 documents the values in a comment
-- only), so this is a COMMENT refresh, not a constraint change. A 'pending_approval' row is
-- INERT: get_individual_by_email / get_individual_by_id filter status='active', so it cannot
-- authenticate, hold a session, or be resolved by any control-plane reader — fail-closed by
-- construction rather than by a new check at every call site.
COMMENT ON COLUMN public.individual_users.status IS
    'active | pending_approval | suspended | disabled. ''pending_approval'' = created by a gated '
    '/me/signup and awaiting a global-admin decision (migration 174): no seat, no per-user '
    'schema, no flu_ token, and CANNOT log in (the readers filter status=''active''). Approval '
    'registers the seat, enqueues provisioning, and flips this to ''active''.';
