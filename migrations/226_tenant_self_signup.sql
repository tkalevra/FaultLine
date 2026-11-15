-- 226_tenant_self_signup.sql — PUBLIC TENANT SELF-SIGNUP (trial + held-application queue)
-- — CLOSED-SOURCE SaaS layer.
--
-- THE GAP THIS CLOSES: there was NO way for a stranger to create a TENANT. The only public
-- signup (POST /me/signup, migration 174) provisions an INDIVIDUAL as a seat on the shared
-- 'Singles' house account; tenant creation was operator-only (POST /admin/accounts, behind
-- require_operator). The Team/Enterprise marketing page therefore dead-ended into the wrong
-- product. POST /signup/team (saas/tenant_signup.py) is the missing door, and it needs exactly
-- two things the schema does not yet carry:
--
--   (1) an ACCOUNT-level trial clock, and
--   (2) somewhere to PARK an application that is held for operator approval.
--
-- ── (1) THE ACCOUNT TRIAL ────────────────────────────────────────────────────────────
-- Three trials now exist and they are DELIBERATELY distinct clocks — do not conflate them:
--   * individual_users.trial_ends_at   (migration 178) — the INDIVIDUAL first-month-free trial.
--   * accounts.chatbot_trial_*         (migration 220/222) — the chat-agent FEATURE trial (15d),
--                                       which fail-closes the /v1/agent/chat serve gate on expiry.
--   * accounts.trial_ends_at           (HERE) — the TENANT account trial: the window in which a
--                                       self-signed-up tenant's base + seat charges are waived.
--
-- This one is a BILLING marker, NOT an entitlement gate. Nothing reads it to GRANT capability —
-- a self-signed-up tenant is created as account_kind='team' (NO $1500 base package is seeded;
-- see saas/accounts.create_account) and gets the ordinary Team hard seat cap, so the trial
-- changes nothing about what the tenant can DO. That is deliberate: a column that only ever
-- widens nothing cannot become an authorization bypass if it is misread or unreadable.
--
-- The LENGTH is the operator-tunable public.platform_settings.trial_days (migration 178,
-- default 30, already surfaced in the operator console) — the SAME dial that governs the
-- individual trial, so "first month on us" means ONE thing product-wide and the operator has
-- ONE lever. No new setting.
--
-- The clock starts when the tenant can actually USE the product (at provisioning), never when
-- the application was typed — mirroring saas/user_console.activate_approved_individual, which
-- stamps the individual trial at APPROVAL time so queue latency is never billed to the user.
--
-- ── (2) THE HELD-APPLICATION QUEUE ───────────────────────────────────────────────────
-- public.platform_settings.require_signup_approval (migration 174, default TRUE) must gate this
-- door too. The individual path holds an application by writing the login row 'pending_approval'
-- and queueing public.signup_approvals against it. That table CANNOT carry a tenant application:
-- its individual_id is `UUID NOT NULL REFERENCES public.individual_users(id)`, and a held tenant
-- signup has no individual_users row — by design, since holding must create NO account, NO seat,
-- NO login and NO token. (Its approver_realm CHECK already admits 'tenant', but that column names
-- WHO APPROVES — a future tenant-scoped seat queue — not what is being approved. A new tenant is
-- admitted to the PLATFORM, so its approver realm is still the operator.)
--
-- So the application is parked in its OWN table. It deliberately MIRRORS public.signup_approvals'
-- state machine and contracts so there is still exactly ONE approval idiom in the product:
--   * pending → approved | denied | expired, single-decision enforced by one guarded UPDATE;
--   * the DASHBOARD QUEUE is authoritative, email only NOTIFIES (degrades gracefully with no SMTP);
--   * the same 5-day TTL (saas/signup_approvals.APPROVAL_TTL_DAYS) with a lazy expiry sweep.
-- Keeping it separate means the existing individual queue — its readers, its approve/deny state
-- machine, and the operator panel that drives them — is not touched at all.
--
-- The argon2id password_hash is parked here because the applicant chose their password at apply
-- time and must not be asked again on approval. It is the SAME hash the login would have carried
-- (saas/dashboard_auth.hash_password); no plaintext is ever stored, logged or emailed. The row is
-- deleted-by-decision, not kept forever: an approved row keeps only the resulting account_id.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path — additive; does not weaken per-tenant isolation. Idempotent; safe to re-run.

-- ── (1) ACCOUNT TRIAL CLOCK ──────────────────────────────────────────────────────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS trial_ends_at    TIMESTAMPTZ NULL;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS trial_source     TEXT NULL;

-- Only accounts actually ON a trial matter to the (future) expiry reaper / operator oversight.
CREATE INDEX IF NOT EXISTS idx_accounts_trial_ends_at
    ON public.accounts (trial_ends_at)
    WHERE trial_ends_at IS NOT NULL;

COMMENT ON COLUMN public.accounts.trial_started_at IS
    'TENANT account trial start (NULL = never on a trial). Stamped at PROVISIONING time by '
    'saas/accounts.start_account_trial — for an approval-held self-signup that is the moment of '
    'APPROVAL, not of application, so queue latency is never billed to the customer.';
COMMENT ON COLUMN public.accounts.trial_ends_at IS
    'TENANT account trial expiry (NULL = no trial). Window length comes from the operator-tunable '
    'public.platform_settings.trial_days (default 30) — the SAME dial as the individual trial. '
    'This is a BILLING marker, NOT an entitlement gate: nothing reads it to grant capability, so '
    'an unreadable/misread value can never widen access. Distinct from chatbot_trial_expires_at '
    '(migration 220), which DOES gate the chat-agent serve path on its own 15-day clock.';
COMMENT ON COLUMN public.accounts.trial_source IS
    'How the trial was started (e.g. ''tenant_self_signup'', or an operator principal) — '
    'attribution only, never read by a gate.';

-- ── (2) TENANT_SIGNUP_REQUESTS — the held tenant-application queue ───────────────────
CREATE TABLE IF NOT EXISTS public.tenant_signup_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- What they asked for. company_name becomes accounts.name; requested_slug is the guarded,
    -- DNS-safe derivation (saas/admin._safe_slug_from_name) — create_account auto-uniquifies it.
    company_name   TEXT NOT NULL,
    requested_slug TEXT,
    -- The prospective OWNER login. email becomes dashboard_users.username (globally UNIQUE), so
    -- an address already holding a tenant login is refused at apply time, not at approval time.
    email          TEXT NOT NULL,
    -- argon2id ($argon2id$...) — the hash the login will carry verbatim. NEVER a plaintext.
    password_hash  TEXT NOT NULL,
    display_name   TEXT,
    -- The billing package to create. 'team' ONLY today: create_account seeds the immutable $1500
    -- base package for kind='tenant', and auto-seeding a $1500 contract line for a stranger on a
    -- free trial would be a phantom MRR row. Enterprise stays sales-led (operator-created).
    account_kind   TEXT NOT NULL DEFAULT 'team',
    status         TEXT NOT NULL DEFAULT 'pending', -- pending | approved | denied | expired
    requested_ip   TEXT,                            -- request source IP (audit / abuse triage)
    decided_by     TEXT,                            -- approver identity (operator principal)
    decided_at     TIMESTAMPTZ,
    account_id     UUID,                            -- the account created ON APPROVAL (else NULL)
    expires_at     TIMESTAMPTZ NOT NULL,            -- auto-expire window (APPROVAL_TTL_DAYS)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT tenant_signup_requests_kind_chk
        CHECK (account_kind IN ('team', 'tenant')),
    CONSTRAINT tenant_signup_requests_status_chk
        CHECK (status IN ('pending', 'approved', 'denied', 'expired'))
);

-- Queue read: pending rows, newest first.
CREATE INDEX IF NOT EXISTS idx_tenant_signup_requests_status
    ON public.tenant_signup_requests (status, created_at DESC);
-- ONE live application per address: a second apply on a pending address is a 409, so a queue can
-- never fill with duplicates of the same prospect (and cannot be used to spam the operator).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_signup_requests_pending_email
    ON public.tenant_signup_requests (lower(email))
    WHERE status = 'pending';

COMMENT ON TABLE public.tenant_signup_requests IS
    'PUBLIC TENANT self-signup applications HELD for operator approval (control-plane public.*). '
    'One row per held POST /signup/team while platform_settings.require_signup_approval is TRUE. '
    'A held application has NO account, NO dashboard login, NO seat, NO key and NO session — the '
    'row IS the whole application. Mirrors public.signup_approvals (migration 174) deliberately: '
    'same pending→approved|denied|expired machine, same queue-is-authoritative/email-only-notifies '
    'contract, same TTL. Separate table because signup_approvals.individual_id is NOT NULL FK to '
    'public.individual_users, which a tenant application has none of. Written by '
    'saas/tenant_signup.py.';
COMMENT ON COLUMN public.tenant_signup_requests.password_hash IS
    'argon2id hash of the password the applicant chose at APPLY time, carried verbatim onto the '
    'owner login at approval so they are never asked twice. No plaintext is stored anywhere.';
COMMENT ON COLUMN public.tenant_signup_requests.account_kind IS
    'Billing package to create on approval. ''team'' ($0 base, 15-seat cap) is the ONLY self-serve '
    'kind: create_account seeds the immutable $1500/5-seat base package for ''tenant'', which must '
    'never be auto-granted to a self-signup on a free trial. Enterprise remains operator-created.';
