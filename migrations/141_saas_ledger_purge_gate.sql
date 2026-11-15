-- Migration 141: allow an AUTHORIZED operator account-purge to cascade-delete the ledger
-- Date: 2026-07-05
-- Purpose: the hard-delete of a TRASHED account (saas.accounts.delete_account) does
--          `DELETE FROM public.accounts`, which ON DELETE CASCADE fans out to every child
--          table — including public.account_ledger. But the append-only trigger (migration
--          139) RAISEs on ANY delete, so the cascade blew up ("account_ledger is append-only").
--
--          Fix: the trigger keeps UPDATE ALWAYS blocked (a billing line is immutable), and
--          keeps DELETE blocked for every normal path — EXCEPT when the transaction sets the
--          purge flag `faultline.account_purge = 'on'` (SET LOCAL, so it is scoped to that one
--          transaction and auto-reverts). delete_account is the ONLY caller that sets it, and
--          only after its operator + slug-confirm + must-be-archived guards. Tamper-deletes
--          from any other code path still find the flag unset and are rejected, so the
--          append-only guarantee for normal operation is unchanged.
--
-- CLOSED-SOURCE SaaS layer. Idempotent (CREATE OR REPLACE; the trigger from 139 is unchanged
-- and simply picks up the new function body).

CREATE OR REPLACE FUNCTION public.account_ledger_append_only()
RETURNS trigger AS $$
BEGIN
    -- A billing line is immutable: UPDATE is NEVER permitted.
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'account_ledger is append-only (UPDATE blocked)';
    END IF;
    -- DELETE is blocked too — EXCEPT during an authorized operator account-purge, which sets
    -- the transaction-local flag faultline.account_purge='on'. That is the ONLY sanctioned
    -- deletion path (hard-deleting a trashed account cascades its ledger rows away with it).
    -- current_setting(..., true) => missing_ok: unset flag reads as NULL, never an error, so
    -- every normal write path (flag unset) still hits the RAISE below.
    IF TG_OP = 'DELETE' THEN
        IF current_setting('faultline.account_purge', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'account_ledger is append-only (DELETE blocked)';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
