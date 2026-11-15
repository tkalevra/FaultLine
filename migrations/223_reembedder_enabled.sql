-- 223_reembedder_enabled.sql — GLOBAL re-embedder enable/disable switch — CLOSED-SOURCE SaaS.
--
-- THE CONTRACT (read by the re-embedder back-end, src/re_embedder/embedder.py):
--   public.platform_settings.re_embedder_enabled  BOOLEAN NOT NULL DEFAULT TRUE
--
-- Default TRUE = current behavior UNCHANGED. The re-embedder loop reads this flag and STOPS
-- system-wide (no staged-fact promotion, no Class-C expiry, no cortex-collection embedding, no
-- Qdrant reconciliation) when it resolves FALSE. This is a PLATFORM-LEVEL (operator) control,
-- NOT per-account: one switch for every tenant.
--
-- Control plane: saas/platform_settings.py::re_embedder_enabled() (5s-TTL cached, fail-CLOSED
-- to False=stop when the control plane is unreadable AND nothing is cached — the re-embedder has
-- no useful work without PostgreSQL anyway, so resolving STOP is GPU-protective and cost-free,
-- and mirrors the offline/api_addon kill-switch posture). Operator endpoints:
-- saas/admin.py GET/POST /admin/reembedder (require_operator-gated, audited from->to).
--
-- THE CONTRACT for the back-end team: the re_embedder must stop system-wide when
-- saas.platform_settings.re_embedder_enabled() returns FALSE (wiring NOT implemented here).
--
-- Control-plane public.* ONLY. Idempotent; safe to re-run.

ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS re_embedder_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Guarded backfill: the NOT NULL DEFAULT TRUE sets the singleton to TRUE on ALTER, but set it
-- explicitly so the row carries the contract value even if a prior partial state left it NULL
-- (the guard preserves any explicit operator override).
UPDATE public.platform_settings
   SET re_embedder_enabled = TRUE
 WHERE singleton = TRUE AND re_embedder_enabled IS NULL;

COMMENT ON COLUMN public.platform_settings.re_embedder_enabled IS
    'GLOBAL re-embedder enable/disable switch (default TRUE = processing ON for all tenants). '
    'The re-embedder back-end STOPS system-wide when this is FALSE. Fail-CLOSED (FALSE=stop) on '
    'an unreadable control plane. Read via saas/platform_settings.re_embedder_enabled().';
