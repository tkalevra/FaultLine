-- 233_agent_pairing.sql — BROWSER PAIRING for the appliance's "Connect to FaultLine"
-- button, so the delegate mint works for EVERY MFA factor — CLOSED-SOURCE SaaS.
--
-- THE DEFECT THIS EXISTS TO FIX. ``POST /v1/auth/tenant-delegate`` (saas/agent_proxy.py)
-- REIMPLEMENTS the tenant login as ONE request: email + password + TOTP in a single body. That
-- shape can only ever support factors that fit in one round trip, so its MFA leg is TOTP-only.
-- WebAuthn/passkeys are challenge/response — a server-issued challenge must be signed by the
-- authenticator and returned — and therefore can NEVER fit that shape. MEASURED in production:
-- the SeedTenant tenant admin has exactly ONE enabled factor, ``factor_type='webauthn'``
-- (a Bitwarden passkey, label "Optimus-FaultLine"), and no TOTP secret at all. That login could
-- not mint a delegate, which made the ENTIRE appliance connect flow (list seats → bind a seat →
-- grant the trial) unreachable for them. Migration 230 shipped a deliberately PASSKEY-FIRST
-- enrolment prompt, so we were actively steering users into the one factor that broke this.
--
-- THE FIX IS NOT "ADD WEBAUTHN TO THE SINGLE REQUEST". It is to STOP DUPLICATING AUTH. The
-- tenant console already implements the complete multi-factor login (/tenant/login →
-- /tenant/mfa/verify | /tenant/mfa/send | /tenant/mfa/webauthn/options+verify), it is the ONE
-- place credentials are verified, and it fails CLOSED. So the appliance now opens a browser
-- pop-up at the REAL console, the human authenticates there with WHATEVER factor their account
-- actually has, and the resulting authenticated SESSION is exchanged for the same short-lived,
-- account-scoped delegate token. Every factor that works in the console works here, including
-- every factor added in future, with no change to this flow.
--
-- WHY A TABLE IS NEEDED AT ALL — i.e. why the pop-up does not just hand the token back.
-- The pop-up runs on the CONSOLE origin; the appliance runs on a DIFFERENT origin (a LAN box, a
-- container, sometimes no browser-reachable origin at all). The two obvious browser transports
-- are both unsafe:
--   * ``window.opener.postMessage(token, '*')`` hands a bearer token to WHOEVER opened the
--     pop-up. Any web page could open our console pop-up and, for a user with a live console
--     session, silently harvest a delegate. CSRF does not help: the minting page would be our
--     OWN JS, same-origin — a textbook confused deputy.
--   * a ``redirect_uri`` supplied by the caller is an open token-exfiltration redirect unless it
--     is allow-listed, and the appliance's address is not knowable in advance.
-- So we use the shape designed for exactly this problem (RFC 8628 device authorization grant):
-- the appliance pre-registers a PAIRING here, keeps a high-entropy ``device_code`` that NEVER
-- touches the browser, sends only the non-secret ``pairing_id`` through the pop-up URL, and then
-- polls FaultLine directly for the result. The delegate token therefore never enters the
-- browser, never crosses an origin, and needs no CORS, no postMessage and no redirect allow-list.
--
-- WHAT THIS TABLE IS AND IS NOT. It is a SHORT-LIVED HANDOFF, not a credential store and not a
-- session. A row holds, for at most ``SAAS_AGENT_PAIRING_TTL`` seconds (default 300):
--   pending  → an appliance has asked to be connected; nobody has approved anything.
--   approved → a human completed a FULL console login (MFA included) and pressed Approve. The
--              row now records WHICH login/account approved it — copied from the SIGNED SESSION
--              server-side, never from a request body.
--   claimed  → the appliance presented the matching device_code once and the delegate was minted.
-- It stores NO password, NO session token and NO delegate token. The delegate is minted at CLAIM
-- time from ``approved_dashboard_user_id`` / ``approved_account_id``, so the appliance receives a
-- FULL-TTL token rather than one whose 300s clock has been burning during the human's login.
--
-- WHY THE DEVICE CODE IS HASHED. Same posture as public.mfa_factors.pending_code_hash: the
-- plaintext exists only in the appliance's memory and in the one response that created it. A
-- database read (backup, replica, incident) yields nothing claimable. The hash is HMAC-SHA256
-- under the app session secret (saas/agent_pairing._device_hash), so a stolen dump cannot even be
-- brute-forced offline without the secret — and the code is 256 bits of CSPRNG anyway.
--
-- THE SECURITY INVARIANTS THIS SCHEMA ENFORCES, so the code cannot forget them:
--   * approved_dashboard_user_id + approved_account_id are FKs with ON DELETE CASCADE — a
--     deleted login or account cannot leave a claimable pairing behind.
--   * status CHECK pins the state machine to the three legal values; every transition in
--     saas/agent_pairing.py is a conditional UPDATE (``WHERE status = <previous>``) so two
--     concurrent claims cannot both win and a pairing is single-use by construction.
--   * expires_at is NOT NULL and every read filters on it, so an abandoned pop-up cannot leave a
--     pairing approvable (or claimable) indefinitely.
--   * an approved row MUST carry both approval columns (CHECK), so "approved by nobody" — the
--     state that would mint an unscoped delegate — is unrepresentable.
--
-- WHAT THIS DOES NOT CHANGE. Nothing about how a second factor is VERIFIED:
-- saas/mfa.require_mfa_decision stays fail-CLOSED, /tenant/login still refuses to issue a session
-- when MFA is required and unsatisfied, and the ratelimit lockouts on the password and MFA-verify
-- steps are untouched. A pairing can only ever be approved BY a session, and a session only
-- exists when the console's own MFA gate has already been satisfied — so this flow cannot mint a
-- delegate the console itself would not have granted. The delegate's scope and TTL are unchanged
-- (account-scoped, AGENT_DELEGATE_TTL_SECONDS). The legacy single-request TOTP path stays for
-- headless/API integrators who have no browser; it simply stops being the ONLY path.
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS public.agent_pairings (
    pairing_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- HMAC-SHA256(pairing_id:device_code) under the app session secret. NEVER the plaintext.
    device_code_hash           TEXT NOT NULL,
    -- Free-text appliance label shown on the approval screen ("Optimus front-desk agent").
    -- UNTRUSTED, attacker-controllable input: the console escapes it, and it is advisory only —
    -- nothing authorizes off it.
    label                      TEXT,
    -- Best-effort source of the /start call, for the approval screen + abuse forensics.
    requester_ip               TEXT,
    status                     TEXT NOT NULL DEFAULT 'pending',
    -- Filled ONLY from a verified session at approve time (saas/tenant_console).
    approved_dashboard_user_id UUID REFERENCES public.dashboard_users(id) ON DELETE CASCADE,
    approved_account_id        UUID REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    approved_username          TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at                 TIMESTAMPTZ NOT NULL,
    approved_at                TIMESTAMPTZ,
    claimed_at                 TIMESTAMPTZ,
    CONSTRAINT agent_pairings_status_chk
        CHECK (status IN ('pending', 'approved', 'claimed')),
    -- "approved by nobody" is the one state that could mint an unscoped delegate. Make it
    -- unrepresentable rather than relying on every future reader to check.
    CONSTRAINT agent_pairings_approval_complete_chk
        CHECK (status = 'pending'
               OR (approved_dashboard_user_id IS NOT NULL AND approved_account_id IS NOT NULL))
);

COMMENT ON TABLE public.agent_pairings IS
    'Short-lived browser-pairing handoffs for the agent appliance''s "Connect to FaultLine" '
    'button (RFC 8628-shaped device flow). Holds NO password, session or delegate token: an '
    'approved row records only WHICH login/account approved it, copied from the signed console '
    'session, and the delegate is minted at claim time. Rows live at most '
    'SAAS_AGENT_PAIRING_TTL seconds (default 300) and are single-use. Written exclusively by '
    'saas/agent_pairing.py.';

COMMENT ON COLUMN public.agent_pairings.device_code_hash IS
    'HMAC-SHA256(pairing_id:device_code) under the app session secret. The plaintext device_code '
    'is 256 bits of CSPRNG, returned ONCE to the appliance and never sent to the browser — it is '
    'what proves a claim comes from the appliance that started the pairing.';

COMMENT ON COLUMN public.agent_pairings.status IS
    'pending → approved → claimed. Every transition is a conditional UPDATE guarded on the '
    'previous status, so a pairing is single-use and two concurrent claims cannot both mint.';

COMMENT ON COLUMN public.agent_pairings.approved_account_id IS
    'The account the delegate will be scoped to. Copied from the SIGNED session at approve time, '
    'never from a request body (the BOLA rule). Operator rows carry no account and therefore can '
    'never reach this column — require_dashboard_session already refuses them.';

-- Claim path: by primary key. These two indexes serve the housekeeping + abuse paths only.
-- Prune sweep (saas/agent_pairing.start runs it opportunistically).
CREATE INDEX IF NOT EXISTS idx_agent_pairings_expires
    ON public.agent_pairings (expires_at);

-- Per-IP flood cap on the UNAUTHENTICATED /start door. Partial: only live pending rows, which
-- is a handful at any moment by construction (300s TTL).
CREATE INDEX IF NOT EXISTS idx_agent_pairings_pending_ip
    ON public.agent_pairings (requester_ip, expires_at)
    WHERE status = 'pending';
