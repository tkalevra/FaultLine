-- 230_mfa_enrolment_prompt.sql — PROMINENT post-sign-in MFA-enrolment prompt + a bounded,
-- SERVER-COUNTED grace period — CLOSED-SOURCE SaaS.
--
-- THE PROBLEM THIS SOLVES. MFA has existed and worked in both human realms for months
-- (public.mfa_factors, saas/mfa.py, the "Security methods" block in the My Account panel), but it
-- is only reachable by opening a modal and scrolling — so nobody turns it on. MEASURED in
-- production: the AcmeCorp tenant admin has no enabled factor; the AcmeCorp one does.
-- That is not an authentication defect, it is a DISCOVERY defect, and the fix is prompting, not
-- a change to how the second factor is verified. NOTHING in this migration touches the
-- verification path (saas/mfa.require_mfa_decision stays fail-CLOSED and untouched).
--
-- THE CONTRACT (read by saas/mfa_prompt.py):
--
--   public.platform_settings.mfa_prompt_grace_logins  INTEGER NOT NULL DEFAULT 10
--       OPERATOR-tunable: how many sign-ins a console user may DEFER the enrolment prompt before
--       it stops being dismissible. Same shape as trial_days / chatbot_trial_days: one operator
--       column on the singleton, read through a clamped fail-SAFE accessor
--       (saas/platform_settings.mfa_prompt_grace_logins, clamp [0, 100], default 10). 0 is a
--       VALID configured value and means "non-dismissible from the very first sign-in".
--
--   public.dashboard_users.mfa_prompt_deferrals    INTEGER NOT NULL DEFAULT 0
--   public.dashboard_users.mfa_prompt_deferred_at  TIMESTAMPTZ
--   public.individual_users.mfa_prompt_deferrals   INTEGER NOT NULL DEFAULT 0
--   public.individual_users.mfa_prompt_deferred_at TIMESTAMPTZ
--       PER-LOGIN counter of how many sign-ins this principal has deferred, and when it last
--       deferred. Both realms get the identical pair so the two consoles cannot drift.
--
-- WHY THE COUNTER LIVES IN POSTGRES AND NOT IN THE BROWSER. The obvious implementation is a
-- localStorage key, and it is worthless: clearing site data resets the grace to zero, so the
-- prompt can be ignored forever by anyone who knows what a browser is. The counter is therefore
-- control-plane state on the principal's own row. A client cannot read it, cannot write it, and
-- cannot forge it — it only ever receives the DECISION (required / dismissible) that the server
-- computed, and the only way to consume a deferral is an authenticated, CSRF-checked POST that
-- the server may refuse.
--
-- WHY `deferred_at` AND NOT JUST A COUNT. The owner's requirement is "ignore it for up to N
-- LOGINS", not "up to N clicks". `mfa_prompt_deferred_at` is compared against the existing
-- `last_login_at` (bumped by dashboard_auth.touch_last_login / user_console._touch_individual_
-- login at every session issue): a deferral only increments the counter when
-- `mfa_prompt_deferred_at < last_login_at`, i.e. at most ONCE per sign-in. Consequences that
-- fall out of that for free:
--   * mashing "remind me later", or reloading the console (F5 restores a session WITHOUT a new
--     login), cannot burn extra grace — the second and later defers of one sign-in are no-ops;
--   * a scripted client cannot exhaust somebody else's grace, and exhausting your OWN is
--     self-punishing anyway (it brings the hard prompt forward, never pushes it back);
--   * "9 of 10 sign-ins used" is literally true rather than approximately true.
--
-- WHAT HAPPENS AT THE THRESHOLD. The prompt becomes NON-DISMISSIBLE — an interstitial the user
-- must act on or sign out of. It is deliberately NOT an account lock and NOT a 403 on the
-- console: the enrolment flow the prompt points at is IN the console, so gating the console on
-- enrolment would strand the user outside the only screen that can satisfy the gate. That is
-- precisely the billing deadlock this codebase just fixed (a fresh owner 403'd out of the one
-- screen that could unlock them), and re-introducing it here would be worse, because the trigger
-- would be a counter rather than a payment. There is always a path forward the user can walk
-- alone: enrol (one click, in the prompt) or sign out. The server refusing to grant an 11th
-- deferral is the enforcement; a lockout is not.
--
-- BACKFILL: none, deliberately. Every existing login starts at 0 deferrals and gets the full
-- grace window on its next sign-in — nobody is retroactively pushed past a threshold by a
-- deploy.
--
-- DEPLOY ORDER MATTERS (same as migration 223 before it). `mfa_prompt_grace_logins` joins
-- saas/platform_settings._COLS, and that SELECT is the one every request's offline check runs
-- through — it fails CLOSED, i.e. OFFLINE, when the row cannot be read. Ship this migration
-- BEFORE (or with) the code, which is what docker-entrypoint.sh already does: migrations apply
-- at container start, ahead of the app. Code-first against an un-migrated database would take
-- the product offline until the column exists.
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.

-- ── 1. The OPERATOR knob (platform singleton) ────────────────────────────────────────────
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS mfa_prompt_grace_logins INTEGER NOT NULL DEFAULT 10;

COMMENT ON COLUMN public.platform_settings.mfa_prompt_grace_logins IS
    'Operator-configurable number of sign-ins a console user may DEFER the MFA-enrolment prompt '
    'before it becomes non-dismissible (default 10). 0 = non-dismissible from the first sign-in. '
    'Fail-SAFE: saas/platform_settings.mfa_prompt_grace_logins clamps to [0,100] and falls back '
    'to 10. Advisory prompting only — it never affects how a second factor is VERIFIED.';

-- ── 2. TENANT-console logins (public.dashboard_users) ────────────────────────────────────
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS mfa_prompt_deferrals INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS mfa_prompt_deferred_at TIMESTAMPTZ;

COMMENT ON COLUMN public.dashboard_users.mfa_prompt_deferrals IS
    'How many SIGN-INS this login has deferred the MFA-enrolment prompt for. Server-counted — a '
    'browser can neither read nor write it. Incremented at most once per sign-in (guarded by '
    'mfa_prompt_deferred_at < last_login_at) and capped at the operator threshold '
    '(platform_settings.mfa_prompt_grace_logins) inside the UPDATE itself, so two concurrent '
    'defers cannot race past it. Reset to 0 once an enabled MFA factor exists. '
    'Read/written via saas/mfa_prompt.py (realm ''dashboard'').';

COMMENT ON COLUMN public.dashboard_users.mfa_prompt_deferred_at IS
    'When the MFA-enrolment prompt was last deferred. Compared against last_login_at so a '
    'deferral counts once per SIGN-IN, not once per click: repeat defers within one session '
    '(including after an F5, which restores a session without a new login) are no-ops.';

-- ── 3. INDIVIDUAL "/me" logins (public.individual_users) ─────────────────────────────────
-- Same gap, same realm-neutral mechanism. Covered here rather than left as a follow-up because
-- the two login paths are structurally identical (argon2 verify → require_mfa_decision → session
-- + touch-last-login) and a per-realm divergence in a security nudge is how one realm silently
-- stops nagging.
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS mfa_prompt_deferrals INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS mfa_prompt_deferred_at TIMESTAMPTZ;

COMMENT ON COLUMN public.individual_users.mfa_prompt_deferrals IS
    'How many SIGN-INS this individual login has deferred the MFA-enrolment prompt for. The '
    'realm-''individual'' twin of public.dashboard_users.mfa_prompt_deferrals — identical '
    'semantics, identical guard, same reader (saas/mfa_prompt.py).';

COMMENT ON COLUMN public.individual_users.mfa_prompt_deferred_at IS
    'When this individual login last deferred the MFA-enrolment prompt. Compared against '
    'last_login_at so a deferral counts once per SIGN-IN.';

-- Partial indexes over the DEFERRING minority only. The prompt read itself is by primary key,
-- so these exist purely to answer the operator/security question "who is still running without
-- a second factor and coasting on the grace window?" without scanning every login row. Tiny by
-- construction: the column defaults to 0, so only principals who have actually deferred appear.
CREATE INDEX IF NOT EXISTS idx_dashboard_users_mfa_prompt_deferred
    ON public.dashboard_users (mfa_prompt_deferrals DESC)
    WHERE mfa_prompt_deferrals > 0;

CREATE INDEX IF NOT EXISTS idx_individual_users_mfa_prompt_deferred
    ON public.individual_users (mfa_prompt_deferrals DESC)
    WHERE mfa_prompt_deferrals > 0;
