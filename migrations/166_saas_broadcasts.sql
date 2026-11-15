-- Migration 166: operator BROADCASTS / announcements — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-11
-- Purpose: back the shared console "alert space" (Part B). The OPERATOR (global super-admin)
--          composes a message targeted at an AUDIENCE; each realm's session resolves the ACTIVE,
--          non-expired, non-dismissed broadcasts for its audience and shows them in the alert
--          strip, each per-principal DISMISSIBLE. Part A (actionable items) is COMPUTED from
--          existing queues (reset_approvals, migration 164) and needs NO new table.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path. Additive; idempotent (IF NOT EXISTS everywhere); safe to re-run. Does NOT touch
-- any per-tenant DATA schema, token, memory, or the search_path rule.
--
-- Read/written with: saas/broadcasts.py (state + audience resolution), guarded at compose time
-- by saas/content_guard.py (profanity guard, fail-closed). Consumed by the alert strip in
-- saas/dashboard/alerts.js via /admin|/tenant|/me alerts endpoints.

-- ── (1) The broadcast messages ─────────────────────────────────────────────────────────
--   audience  — WHO sees it: 'admins' (tenant-admin console) | 'users' (individual console) | 'all'.
--   severity  — the alert-strip band: 'info' | 'warning' | 'critical'.
--   active    — soft on/off; deactivate hides it without deleting the history.
--   expires_at NULL = no auto-expiry (stays until deactivated); otherwise hidden past the timestamp.
CREATE TABLE IF NOT EXISTS public.broadcasts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    author      TEXT NOT NULL,                        -- operator identity (dashboard_user_id / 'operator-key')
    audience    TEXT NOT NULL,                        -- 'admins' | 'users' | 'all'
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',         -- 'info' | 'warning' | 'critical'
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ,                          -- NULL = never auto-expires
    CONSTRAINT broadcasts_audience_chk CHECK (audience IN ('admins', 'users', 'all')),
    CONSTRAINT broadcasts_severity_chk CHECK (severity IN ('info', 'warning', 'critical'))
);
-- Consumer read: ACTIVE broadcasts for an audience, newest first.
CREATE INDEX IF NOT EXISTS idx_broadcasts_active_audience
    ON public.broadcasts (active, audience, created_at DESC);

COMMENT ON TABLE public.broadcasts IS
    'Operator BROADCASTS / announcements (control-plane public.*). One row per message; audience '
    'admins|users|all; severity info|warning|critical; soft active flag + optional expires_at. '
    'Composed OPERATOR-ONLY, profanity-guarded at the compose seam (saas/content_guard.py). '
    'Consumed by the shared console alert strip; per-principal dismissible via broadcast_dismissals.';

-- ── (2) Per-principal dismissals ────────────────────────────────────────────────────────
-- A consumer dismisses a broadcast for THEMSELVES only. principal_id is the SESSION principal
-- (server-derived: operator/tenant dashboard_user_id, or individual_id) — NEVER client input.
-- One row per (principal, broadcast); re-dismiss is idempotent (ON CONFLICT DO NOTHING).
CREATE TABLE IF NOT EXISTS public.broadcast_dismissals (
    principal_id  TEXT NOT NULL,                      -- session principal (dashboard_user_id | individual_id)
    broadcast_id  UUID NOT NULL REFERENCES public.broadcasts(id) ON DELETE CASCADE,
    dismissed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (principal_id, broadcast_id)
);
CREATE INDEX IF NOT EXISTS idx_broadcast_dismissals_broadcast
    ON public.broadcast_dismissals (broadcast_id);

COMMENT ON TABLE public.broadcast_dismissals IS
    'Per-principal dismissal of a broadcast (control-plane public.*). principal_id is the SERVER-'
    'derived session principal (dashboard_user_id | individual_id), never client input — a consumer '
    'can only dismiss for itself (BOLA-safe). Written by saas/broadcasts.py.';
