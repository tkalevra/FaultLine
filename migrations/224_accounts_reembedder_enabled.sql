-- 224_accounts_reembedder_enabled.sql — PER-TENANT re-embedder enable/disable switch — CLOSED-SOURCE SaaS.
--
-- THE CONTRACT (read by the re-embedder back-end, src/re_embedder/embedder.py):
--   public.accounts.re_embedder_enabled  BOOLEAN NOT NULL DEFAULT TRUE
--
-- Default TRUE = current behavior UNCHANGED for every tenant. This is the PER-TENANT control;
-- it COMPOSES with the GLOBAL switch (public.platform_settings.re_embedder_enabled, migration 223)
-- via an AND — the re-embedder processes a tenant iff BOTH are TRUE:
--
--     process_tenant  <=>  platform_settings.re_embedder_enabled() == TRUE
--                      AND  accounts.re_embedder_enabled(account_id) == TRUE
--
-- When this per-tenant flag is FALSE the re-embedder SKIPS that tenant (no staged-fact promotion,
-- no Class-C expiry, no cortex-collection embedding, no Qdrant reconciliation for its seats). The
-- GLOBAL flag remains the system-wide kill; this flag lets a single tenant opt its OWN background
-- processing off without affecting anyone else.
--
-- Control plane: saas/accounts.py::re_embedder_enabled(account_id) (short-TTL cached, fail-CLOSED
-- to False=skip when the control plane is unreadable AND nothing was ever cached). Tenant endpoints:
-- saas/tenant_console.py GET/POST /tenant/reembedder (require_dashboard_session-gated, BOLA-safe —
-- account_id is server-derived from the signed session; audited from->to in saas_audit_log).
--
-- THE CONTRACT for the back-end team: skip a tenant when
-- accounts.re_embedder_enabled = FALSE (composed AND with the global platform_settings flag).
--
-- Control-plane public.* ONLY. Idempotent; safe to re-run.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS re_embedder_enabled BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN public.accounts.re_embedder_enabled IS
    'PER-TENANT re-embedder enable/disable switch (default TRUE = processing ON for this tenant). '
    'Composes with the GLOBAL platform_settings.re_embedder_enabled via AND: the re-embedder '
    'processes a tenant iff BOTH are TRUE. The re-embedder SKIPS a tenant where this is FALSE. '
    'Fail-CLOSED (FALSE = skip) on an unreadable control plane. '
    'Read via saas/accounts.re_embedder_enabled(account_id).';
