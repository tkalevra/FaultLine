-- 248_account_term_of_record.sql
-- CLOSED-SOURCE SaaS layer.
--
-- WHY THIS EXISTS: THE CONFIGURED CONTRACT TERM WAS BEING APPLIED RETROACTIVELY.
-- ─────────────────────────────────────────────────────────────────────────────────────
-- ``saas/tenant_provisioning.py::_apply_self_serve_term`` writes ``tenant_term_months()`` — the
-- LIVE operator setting (public.platform_settings.contract_term_months, migration 246) — and it
-- runs on ``invoice.paid``, i.e. on EVERY RENEWAL, EVERY MONTH, FOREVER (the module documents that
-- "self-heal" posture deliberately). So an operator who moved the term from 6 to 12 silently
-- re-termed every existing paying tenant at their next renewal, against the term each of them
-- actually signed.
--
-- The account row is the RECORD of the term that account runs on. Once a term is of record it must
-- never again be re-derived from live configuration. The blocker was that
-- ``public.accounts.term_months`` is ``NOT NULL DEFAULT 6`` (migration 139), so the column carries
-- NO WAY to tell "6 because that is what this customer bought" from "6 because nobody ever set it".
-- Worse, ``entitlements.seed_base_entitlement`` ends with an UNCONDITIONAL
-- ``UPDATE public.accounts SET term_months = DEFAULT_TERM_MONTHS`` on every renewal, so reading the
-- column after provisioning shows the seed's value, not the account's history.
--
-- These two columns are that missing sentinel. They are the RECORD, not a cache:
--
--   term_months_set_at  NULL  = never deliberately set — a legacy row carrying only the schema
--                               default (or a value written before this migration existed).
--                       NOT NULL = the term is OF RECORD as of this instant. Live configuration
--                               MUST NOT touch it again.
--   term_months_source  who/what put the term of record. See the CHECK for the vocabulary.
--
-- FORWARD-ONLY BY DESIGN, AND NOT BACKFILLED. Every existing row starts NULL, which is truthful:
-- we genuinely do not know whether an existing account's 6 was chosen or defaulted. Backfilling a
-- ``set_at`` here would be inventing history. Instead the application FREEZES a legacy row the
-- first time it meets one (stamping source='legacy_frozen' with the value the account was already
-- running on), so an existing customer's term is pinned to what it has been, never to what the
-- console currently says.
--
-- NOTHING READS THESE COLUMNS AS A GATE ON SERVICE. They govern exactly one decision: whether
-- provisioning may write ``term_months`` from live config. A NULL is safe everywhere else.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS + guarded constraint), and guarded on the table existing so
-- it is independent of apply order in a partially-built control plane.

DO $$
BEGIN
    IF to_regclass('public.accounts') IS NULL THEN
        RAISE NOTICE '248: public.accounts absent — skipping (control plane not built here)';
        RETURN;
    END IF;

    ALTER TABLE public.accounts
        ADD COLUMN IF NOT EXISTS term_months_set_at TIMESTAMPTZ;
    ALTER TABLE public.accounts
        ADD COLUMN IF NOT EXISTS term_months_source TEXT;

    COMMENT ON COLUMN public.accounts.term_months_set_at IS
        'When term_months was put OF RECORD for this account. NULL = never deliberately set (a '
        'legacy row carrying only the NOT NULL DEFAULT 6, or one written before migration 248). '
        'NOT NULL is a LATCH: saas/tenant_provisioning.py must not re-derive term_months from the '
        'live platform setting once this is stamped. This is what makes a configuration change '
        'apply to NEW sign-ups only — historic terms are accounting and are maintained.';

    COMMENT ON COLUMN public.accounts.term_months_source IS
        'What put term_months of record: ''provision'' = the live configured term at the moment '
        'this subscription was PURCHASED; ''operator'' = a human typed it (preserved and restored '
        'against the unconditional re-stamp in entitlements.seed_base_entitlement); '
        '''legacy_frozen'' = an account that pre-dates migration 248, pinned on its first renewal '
        'to the value it was already running on rather than to current configuration; ''create'' '
        'is reserved for the account-creation door. NULL alongside a NULL set_at = unknown.';

    -- Vocabulary is CLOSED and enforced, because this column is the audit answer to "why does this
    -- customer have this term". A free-text drift here would make the record unreadable. NULL is
    -- permitted (an unstamped legacy row); a NON-NULL value must be one of the four.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = to_regclass('public.accounts')
           AND conname  = 'accounts_term_months_source_valid'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_term_months_source_valid
            CHECK (term_months_source IS NULL
                   OR term_months_source IN ('create', 'provision', 'operator', 'legacy_frozen'));
    END IF;
END $$;

-- Operator-findable: "which accounts still have a floating term?" is a one-index question, and the
-- answer shrinks to zero as legacy accounts are frozen at their renewals. Partial, so it costs
-- nothing once the migration to a fully-stamped estate is complete.
CREATE INDEX IF NOT EXISTS idx_accounts_term_not_of_record
    ON public.accounts (account_id)
 WHERE term_months_set_at IS NULL;
