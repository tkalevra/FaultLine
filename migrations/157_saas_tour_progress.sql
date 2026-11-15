-- Migration 157: guided-onboarding TOUR progress (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-10
--
-- Per-PRINCIPAL, per-realm completion state for the in-console guided tour so the ✓
-- checkmarks (and the "you've already seen the first-run prompt" flag) survive a new
-- browser / device. The frontend keeps a localStorage OPTIMISTIC cache, but THIS table is
-- the source of truth.
--
--   realm         ∈ {operator, tenant, individual} — which of the three human auth realms
--                   the principal belongs to (the session cookie decides; never client input).
--   principal_id  the SIGNED-session subject (operator/tenant dashboard_user id, or the
--                   individual login id) — the stable per-human key. NEVER a tenant user_id
--                   supplied by the client (BOLA-safe: the backend derives it from the cookie).
--   topic_key     the completed tour topic. The reserved sentinel '__firstrun_dismissed__'
--                   records that the principal dismissed/took the one-time first-run prompt.
--
-- Control-plane registry table in public.* (read at request time like accounts /
-- dashboard_users). NEVER seeded into a tenant schema, NEVER read from a tenant search_path —
-- additive, does not weaken per-tenant isolation. Idempotent; safe to re-run. NO-OP when
-- SAAS is off (nothing reads it).

CREATE TABLE IF NOT EXISTS public.tour_progress (
    realm        TEXT        NOT NULL,
    principal_id TEXT        NOT NULL,
    topic_key    TEXT        NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (realm, principal_id, topic_key)
);

-- Guard the realm value set (mirrors the CHECK style of migration 148/139).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tour_progress_realm_check'
    ) THEN
        ALTER TABLE public.tour_progress
            ADD CONSTRAINT tour_progress_realm_check
            CHECK (realm IN ('operator', 'tenant', 'individual'));
    END IF;
END $$;

-- Hot read is "all rows for this (realm, principal_id)" — served by the PK's leading
-- columns, so no extra index is required.
