-- 305_saas_tenant_oauth_identity.sql — OAUTH IDENTITY COLUMNS FOR dashboard_users (BUG-01)
-- CLOSED-SOURCE SaaS layer. Control-plane public.* ONLY — never seeded into a tenant schema.
--
-- THE GAP (BUGS/tenant-oauth-login-20261031): the individual (/me) console signs in with
-- Google/GitHub end-to-end (/me/oauth/*), but the TENANT console (dashboard/tenant.html) had
-- only email+password→MFA. The tenant OAuth door cannot bind a provider identity because
-- dashboard_users carried NO OAuth identity columns — the door was not merely unrendered.
--
-- SHAPE (mirrors the individual lane's identity anchor):
--   * oauth_provider  — the registry key from saas/oauth_login.py ('google'/'github' today;
--     the column stores whatever the registry resolves, never a code literal).
--   * oauth_subject   — the provider's stable account id (Google 'sub' / GitHub numeric id).
--   * ONE partial UNIQUE index on the PAIR, WHERE both are NOT NULL: one provider identity can
--     bind to at most ONE dashboard login (fail-closed bind), and NULL rows (every existing
--     login, and any password-only login forever) are exempt. Partial — not a full index —
--     exactly like the email partials of migration 304.
--
-- NO backfill, NO auto-bind: existing logins bind on their first successful OAuth sign-in
-- (verified provider email → email_canonical/username match, status='active'), and an unknown
-- email is REFUSED — an OAuth round trip must never create a dashboard login.
--
-- Read/written by: saas/tenant_console.py (/tenant/oauth/*). Idempotent; safe to re-run.

ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS oauth_provider TEXT;
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS oauth_subject TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_dashboard_users_oauth_identity
    ON public.dashboard_users (oauth_provider, oauth_subject)
    WHERE oauth_provider IS NOT NULL AND oauth_subject IS NOT NULL;

DO $$
BEGIN
    EXECUTE $c$
        COMMENT ON COLUMN public.dashboard_users.oauth_provider IS
            'OAuth provider registry key (saas/oauth_login.py) this login last bound via '
            '/tenant/oauth/*. NULL = no OAuth identity bound (password-only login). Set ONLY by '
            'a first successful fail-closed bind — never by signup; OAuth never creates a '
            'dashboard login (BUG-01).'
    $c$;
    EXECUTE $c$
        COMMENT ON COLUMN public.dashboard_users.oauth_subject IS
            'The provider''s stable account id (Google sub / GitHub id) for the bound '
            'oauth_provider. The PRIMARY repeat-login match key; the verified email is only a '
            'FALLBACK (loud log) when the subject does not match. NULL = unbound.'
    $c$;
END $$;
