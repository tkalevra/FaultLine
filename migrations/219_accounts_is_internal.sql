-- 219_accounts_is_internal.sql — the "internal/hidden" tenant flag (SPEC §12 dogfooding).
--
-- Some accounts are OPERATOR-INTERNAL (not customer tenants): the dogfooding "eat its own
-- supper" agent, throwaway sandboxes, platform infrastructure seats. These must NOT appear
-- in the normal operator tenant roster (the customer list) — they are reached by a direct
-- link/route instead. This column is the one place that fact lives, so list_accounts can
-- filter them out and a dedicated internal-accounts endpoint can list them.
--
-- DEFAULT FALSE: every existing and new customer account is, by default, a normal roster
-- tenant. Only an explicit operator action (accounts.set_account_internal) flips it. The
-- account stays status='active' (so its seats/brain work normally) — is_internal is purely a
-- ROSTER-VISIBILITY flag, orthogonal to the status lifecycle (suspend/lock/archive).
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). The filter that reads this lives in
-- saas/accounts.list_accounts; the operator write path in saas/accounts.set_account_internal.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS is_internal BOOL NOT NULL DEFAULT FALSE;

-- Index the (rare) TRUE rows so the internal-accounts list is cheap.
CREATE INDEX IF NOT EXISTS idx_accounts_is_internal
    ON public.accounts (account_id) WHERE is_internal = TRUE;
