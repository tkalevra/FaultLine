-- Migration 182: a DEFAULT MFA factor per principal — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-15
-- Purpose: minimum-friction login. Instead of picking a method every time, a principal has ONE
--          default factor; login goes straight to it (the chooser stays as a "use another method"
--          escape). One default per (principal_realm, principal_id), enforced by a partial unique
--          index. The app auto-sets the first method as default and auto-promotes the oldest
--          remaining method if the default is removed, so a default is always present when possible.
--
-- Backfill: give every already-enrolled principal a sensible default (their oldest ENABLED factor)
-- so nobody has to set one. Idempotent (guarded by "no default yet"; re-run is a no-op).

ALTER TABLE public.mfa_factors ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;

-- At most ONE default per principal.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mfa_factors_one_default
    ON public.mfa_factors (principal_realm, principal_id) WHERE is_default;

-- Backfill the oldest enabled factor as default for principals that have enabled factors but no
-- default yet. ROW_NUMBER picks the earliest (created_at, then id) deterministically.
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY principal_realm, principal_id
                              ORDER BY created_at, id) AS rn
    FROM public.mfa_factors f
    WHERE enabled
      AND NOT EXISTS (
          SELECT 1 FROM public.mfa_factors d
          WHERE d.principal_realm = f.principal_realm
            AND d.principal_id = f.principal_id
            AND d.is_default
      )
)
UPDATE public.mfa_factors SET is_default = TRUE
WHERE id IN (SELECT id FROM ranked WHERE rn = 1);
