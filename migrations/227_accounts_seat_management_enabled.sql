-- 227_accounts_seat_management_enabled.sql — PER-TENANT "may this tenant's own admins
-- add/remove seats?" OPERATOR permission — CLOSED-SOURCE SaaS.
--
-- THE CONTRACT (read by the tenant-console gate, saas/tenant_console.py::require_seat_management):
--   public.accounts.seat_management_enabled  BOOLEAN NOT NULL DEFAULT TRUE
--
-- WHY A THIRD LEVER. Seat plumbing already has two, and this is neither of them:
--   1. BUYING capacity   = SPEND        → require_billing_access (can_view_billing, migration 159)
--                                          POST /tenant/capacity, /tenant/capacity/bundle,
--                                          DELETE /tenant/capacity.
--   2. seat_limit (213)  = a NUMERIC CAP → the licensed ceiling every register_seat is checked
--                                          against (402 at the ceiling). Not a permission.
--   3. THIS FLAG         = a PERMISSION  → whether the tenant's OWN admins may CONSUME/RELEASE a
--                                          licensed seat at all. Consuming a seat is configuration
--                                          against already-purchased capacity — free at the moment
--                                          of the click — but it is how a tenant walks its bill up
--                                          to the ceiling it bought, so the operator wants a
--                                          per-tenant switch over it. It does NOT read, imply, or
--                                          change can_view_billing or seat_limit; all three
--                                          compose (a seat create needs the flag ON *and* room
--                                          under the cap).
--
-- The four endpoints this gates (all previously require_dashboard_session ONLY):
--   POST   /tenant/seats                  tenant_create_seat        (human/MCP connection seat)
--   DELETE /tenant/seats/{user_id}        tenant_deprovision_seat
--   POST   /tenant/agent-seats            tenant_create_agent_seat  (chat-agent seat)
--   DELETE /tenant/agent-seats/{user_id}  tenant_delete_agent_seat
-- Untouched on purpose: seat RENAME/label, token issue/revoke, tier move/upgrade, per-seat login
-- and cortex actions — none of them change the seat COUNT, so none of them are seat management.
--
-- DEFAULT TRUE IS DELIBERATE. Every existing tenant can add/remove seats today; a default of
-- FALSE would silently strip an ability they already have on the next deploy. The operator
-- opts a tenant OUT, one tenant at a time.
--
-- FAIL-CLOSED. The gate denies (403 + a saas_audit_log denial row) when the flag is FALSE *or*
-- when the control plane is unreadable — an outage must not hand out a seat-provisioning
-- window. Note the asymmetry with the DEFAULT: the stored default is permissive, the runtime
-- behaviour on an unreadable row is restrictive. Hiding the button in the tenant console
-- (via can_manage_seats on GET /tenant/me) is an AFFORDANCE only; this server gate is the
-- boundary.
--
-- Control plane: saas/accounts.py::get_seat_management(account_id) /
-- set_seat_management_enabled(account_id, enabled, changed_by=...). Uncached by design — the
-- read is one indexed primary-key row on a low-frequency (seat create/delete) path, so there is
-- no cache to invalidate and no staleness window on an operator flip. Operator surface:
-- GET/POST /admin/accounts/{account_id}/seat-management (POST is require_operator; the tenant
-- has NO self-service path to this flag — that is the point).
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS seat_management_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- WHO/WHEN audit for the operator flip (the ledger of record stays saas_audit_log; these two
-- columns are the cheap "current state provenance" the console paints next to the checkbox).
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS seat_management_changed_at TIMESTAMPTZ;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS seat_management_changed_by TEXT;

COMMENT ON COLUMN public.accounts.seat_management_enabled IS
    'PER-TENANT OPERATOR PERMISSION: may this tenant''s OWN admins add/remove seats? '
    '(default TRUE = current behaviour unchanged for every existing tenant). Gates '
    'POST/DELETE /tenant/seats and POST/DELETE /tenant/agent-seats via '
    'saas/tenant_console.require_seat_management. DISTINCT from can_view_billing (migration 159, '
    'which gates BUYING capacity) and from seat_limit (migration 213, a numeric cap): this is a '
    'permission over CONSUMING already-licensed capacity. Fail-CLOSED (deny) when FALSE or when '
    'the control plane is unreadable. Operator-set only — no tenant self-service path. '
    'Read via saas/accounts.get_seat_management(account_id).';

COMMENT ON COLUMN public.accounts.seat_management_changed_at IS
    'When seat_management_enabled was last flipped by an operator (NULL = never changed since '
    'the migration default). Provenance only; saas_audit_log is the record of authority.';

COMMENT ON COLUMN public.accounts.seat_management_changed_by IS
    'Which operator identity last flipped seat_management_enabled (operator username, or '
    '''operator-key'' for the break-glass key). Provenance only.';

-- Partial index on the RESTRICTED set only. Genuinely useful and genuinely tiny: the flag
-- defaults TRUE, so this indexes the handful of opted-out tenants and answers the operator's
-- "which tenants have seat management turned off?" sweep without scanning every account. A
-- full index would be pure overhead — the gate itself reads by account_id (primary key).
CREATE INDEX IF NOT EXISTS idx_accounts_seat_management_off
    ON public.accounts (account_id)
    WHERE seat_management_enabled = FALSE;
