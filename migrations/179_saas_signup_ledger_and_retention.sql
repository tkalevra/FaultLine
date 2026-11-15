-- Migration 179: SELF-SERVE data-retention + repeat-signup ledger — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-15
-- Purpose: three control-plane public.* objects backing the retention + repeat-trial policy for
--          self-serve individual signups. Individual accounts are NOT kept beyond a grace window
--          past the free trial (privacy); repeat signups walk a 3-strikes ladder; the operator
--          gets a periodic digest of the activity.
--
--   (1) public.signup_ledger — the repeat-signup memory that SURVIVES an account purge. Keyed by a
--       SALTED SHA-256 HASH of the normalized email (NOT plaintext — PRIVACY DECISION). When the
--       purge reaper tears down an expired individual's account+data, this row REMAINS so a
--       re-signup on the same address still walks the 3-strikes ladder. Because the reaper deletes
--       the plaintext email with everything else, retaining a one-way hash keeps the abuse signal
--       without holding PII: we never store the address, only a value we can re-derive from an
--       address the applicant re-supplies at attempt time (the cheeky/reject emails send to that
--       freshly-provided address, so no plaintext is retained post-purge).
--
--   (2) public.platform_settings.retention_grace_days — the operator VALVE (default 7). How many
--       days past trial_ends_at an unsubscribed individual is kept before the purge reaper is
--       eligible to delete it. Mirrors the trial_days valve (migration 178) exactly.
--
--   (3) public.retention_report_config — the operator DIGEST config (recipients + a TIME-based
--       cadence + last-send marker). Mirrors public.bench_report_config (migration 176) but the
--       cadence is DAYS-between-sends (a periodic operational digest), not a count-crossing.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path — additive; does not weaken per-tenant isolation. Idempotent (IF NOT EXISTS
-- everywhere); safe to re-run.
--
-- Read/written with: saas/signup_ledger.py, saas/retention.py, saas/retention_report.py,
-- saas/platform_settings.py, saas/user_console.py, saas/admin.py.

-- ── (1) SIGNUP_LEDGER — the purge-surviving repeat-signup memory (salted email HASH) ──────────
CREATE TABLE IF NOT EXISTS public.signup_ledger (
    -- Salted SHA-256 hex of the NORMALIZED (trim+lowercase) email. NOT plaintext (privacy). The
    -- salt lives in an env secret (SAAS_SIGNUP_LEDGER_SALT, falling back to SAAS_SESSION_SECRET),
    -- so a stolen DB alone cannot brute the (already low-entropy) address space without the salt.
    email_hash    TEXT PRIMARY KEY,
    -- How many times this address has SIGNED UP (or been rejected). 1 = first, 2 = cheeky-allowed,
    -- 3+ = rejected. The 3-strikes ladder keys on this counter.
    attempt_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- The outcome of the MOST RECENT attempt: allowed | allowed_cheeky | rejected. Drives the
    -- digest breakdown and lets an operator audit an address's trajectory.
    last_outcome  TEXT NOT NULL DEFAULT 'allowed',
    -- OPTIONAL coarse abuse signal — a salted hash of the request IP (never the raw IP). Purely
    -- advisory (an operator triage hint); NEVER a gate (NAT/CGNAT/shared-IP make raw-IP blocking
    -- wrong). NULL when the source IP could not be resolved.
    ip_hash       TEXT,
    CONSTRAINT signup_ledger_outcome_chk
        CHECK (last_outcome IN ('allowed', 'allowed_cheeky', 'rejected'))
);
-- Digest reads: recent activity + per-outcome counts over a window.
CREATE INDEX IF NOT EXISTS idx_signup_ledger_last_seen
    ON public.signup_ledger (last_seen_at DESC);

COMMENT ON TABLE public.signup_ledger IS
    'Repeat-signup memory that SURVIVES an individual account purge (control-plane public.*). '
    'Keyed by a SALTED SHA-256 HASH of the normalized email — NOT plaintext (privacy): the purge '
    'reaper deletes the account + all PII, this row persists so a re-signup on the same address '
    'still walks the 3-strikes ladder (attempt 1 = allowed, 2 = allowed_cheeky, 3+ = rejected). '
    'Written by saas/signup_ledger.py from saas/user_console.py::me_signup + the purge reaper.';
COMMENT ON COLUMN public.signup_ledger.email_hash IS
    'Salted SHA-256 hex of trim+lowercase email. Salt = env SAAS_SIGNUP_LEDGER_SALT (or '
    'SAAS_SESSION_SECRET). Never store or log the plaintext address.';

-- ── (2) platform_settings.retention_grace_days — the operator grace-window VALVE ──────────────
-- Days past trial_ends_at that an UNSUBSCRIBED individual is kept before the purge reaper may
-- delete it (privacy: we do not hold a person's data while they decide). Default 7. The app clamps
-- reads to [0, 365] (defense in depth); the operator write endpoint rejects out-of-range with 400.
-- 0 = purge as soon as the trial lapses (no grace); the reaper stays DRY-RUN/OFF by default
-- regardless (SAAS_RETENTION_PURGE_ENABLED), so a small grace value can never surprise-delete.
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS retention_grace_days INTEGER NOT NULL DEFAULT 7;

COMMENT ON COLUMN public.platform_settings.retention_grace_days IS
    'Operator-configurable grace window in DAYS past trial_ends_at before an unsubscribed '
    'individual is eligible for the retention purge reaper (default 7; 0 = no grace). Clamped to '
    '[0,365] by the app; the operator write endpoint rejects out-of-range with 400. The reaper '
    'itself is DRY-RUN/OFF by default (env SAAS_RETENTION_PURGE_ENABLED) — this valve only bounds '
    'WHICH accounts are candidates, never whether deletion is armed.';

-- ── (3) RETENTION_REPORT_CONFIG — the operator DIGEST config (recipients + TIME cadence) ──────
-- Mirrors public.bench_report_config (migration 176): a singleton (id = 1) holding recipients[] +
-- cadence + last-send marker. The cadence here is DAYS BETWEEN SENDS (a periodic operational
-- digest), not a count-crossing — the background loop fires when NOW() - last_report_at >= cadence.
CREATE TABLE IF NOT EXISTS public.retention_report_config (
    id             INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),   -- singleton latch
    recipients     TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],        -- operator email addresses
    cadence_days   INTEGER NOT NULL DEFAULT 7,                     -- email every X days (>= 1)
    last_report_at TIMESTAMPTZ,                                    -- last successful auto-send
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT retention_report_cadence_chk CHECK (cadence_days >= 1)
);

-- Seed the singleton (idempotent) so the read side always finds a row.
INSERT INTO public.retention_report_config (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE public.retention_report_config IS
    'Operator DIGEST config for the self-serve retention/repeat-signup report (control-plane '
    'public.*). Singleton row: recipients[] + cadence_days (email every X days) + last_report_at. '
    'Mirrors public.bench_report_config (migration 176) with a TIME cadence. Written by '
    'saas/retention_report.py; managed from the operator console (saas/admin.py).';
