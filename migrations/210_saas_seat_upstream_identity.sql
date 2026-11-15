-- Migration 210: UPSTREAM (OpenWebUI-supplied) SEAT IDENTITY — display name + email.
-- Date: 2026-08-01
--
-- THE PROBLEM. A tenant wires OpenWebUI ONCE with an account key (flk_). Every signed-in
-- OWUI user then auto-provisions as a seat BY UUID (saas/seats.py::auto_provision_seat,
-- reached from bind_tenant), because the only thing the MCP transport consumed was
-- `X-OpenWebUI-User-Id`. The Seats page therefore rendered a column of BARE UUIDs with no
-- way to tell which is which person — the first question every new tenant asks after
-- connecting, answerable today only by hand-diffing against OpenWebUI's own Admin Panel.
-- Miserable at 5 users, unusable at 50.
--
-- WHAT THIS STORES. OpenWebUI forwards the signed-in user's display name and email
-- alongside the id when ENABLE_FORWARD_USER_INFO_HEADERS=true. Those two values are
-- captured at the transport and parked here, next to the seat they identify:
--
--   owui_name            the upstream DISPLAY NAME  (X-OpenWebUI-User-Name)
--   owui_email           the upstream EMAIL         (X-OpenWebUI-User-Email)
--   owui_identity_seen_at  when either was last observed/refreshed
--
-- ⚠️ THIS IS UPSTREAM TRUTH, NOT A TENANT-EDITABLE FIELD. It mirrors what the tenant's own
-- OpenWebUI says about that user and is refreshed whenever a differing value arrives (a user
-- renaming themselves in OWUI must not strand a stale name here). The tenant-owned, editable
-- seat name is the EXISTING `label` column and is untouched by this migration and by every
-- write path it adds — the two are deliberately separate fields with separate authorities.
--
-- ⚠️ THIS IS PII. An email/display name is personal data arriving from an upstream system.
-- Handling rules enforced in code (saas/seats.py, saas/tenant_console.py, src/mcp/http_server.py):
--   * NEVER logged — no log line, audit detail, or event payload carries either value.
--   * Rendered ONLY to the owning account's admins, via the session-authenticated
--     GET /tenant/seats (account_id derived from the signed session = BOLA-safe). A
--     seat-holder cannot enumerate colleagues; the operator console is NOT given the field.
--   * Length-capped and control-character-stripped at capture — treated as hostile input.
--
-- Both columns are NULLABLE with NO default and NO backfill: a seat whose upstream sends no
-- identity (older OWUI, ENABLE_FORWARD_USER_INFO_HEADERS off, a non-OWUI client, a
-- manually-added seat) stays NULL and renders exactly as it does today ("unlabeled seat").
-- "If available" is the contract — absent identity is the normal case, never an error.
--
-- CONTROL-PLANE fields in public.account_users (read at request time like migrations
-- 139/148/159/161). NEVER seeded into a tenant schema, NEVER read from a tenant search_path
-- — additive, and does not weaken the per-tenant DATA rule (SET search_path WITHOUT public).
-- `public.account_users` is created by migration 131 and exists ONLY in public; it has no
-- counterpart in src/provisioning/templates/user_schema.sql, so there is no template half to
-- keep in step (verified: the template contains no reference to account_users).
--
-- Idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.

ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS owui_name TEXT;

ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS owui_email TEXT;

ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS owui_identity_seen_at TIMESTAMPTZ;

COMMENT ON COLUMN public.account_users.owui_name IS
    'PII. Upstream display name from X-OpenWebUI-User-Name. Read-only mirror of the tenant''s '
    'OpenWebUI; NOT the tenant-editable seat name (that is account_users.label). Never logged; '
    'rendered only to the owning account''s admins.';
COMMENT ON COLUMN public.account_users.owui_email IS
    'PII. Upstream email from X-OpenWebUI-User-Email. Read-only mirror of the tenant''s '
    'OpenWebUI. Never logged; rendered only to the owning account''s admins.';
COMMENT ON COLUMN public.account_users.owui_identity_seen_at IS
    'When owui_name/owui_email were last observed or refreshed from an upstream request.';
