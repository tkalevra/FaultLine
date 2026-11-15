-- Migration 245: EXTERNALLY BILLED ACCOUNTS — "we ARE charging this account, on someone
--                else's rail, so the internal biller must stand down"
--                (CLOSED-SOURCE SaaS layer)
-- Date: 2026-08-12
--
-- ── WHY THIS EXISTS: A COMP AND AN EXTERNAL RAIL ARE DIFFERENT AXES ──────────────────────
--
-- Migration 211 landed COMP, and comp means exactly one thing (owner, verbatim):
--
--     "a comped account 'complimentary' is not billed, it's written off with a signature and
--      reason either based on a specific date, or indefinitely, ie. my AcmeCorp Tenant or
--      testing tenants should by default be set as 'complimentary[comp]' accounts"
--
-- COMP = COMPLIMENTARY = A DELIBERATE, AUTHORISED WRITE-OFF. Signature (comp_by), reason
-- (comp_reason, CHECK-enforced non-empty), and either an expiry (comp_until) or indefinite.
-- Nothing in this migration changes, weakens, or reinterprets that. Comp stays exactly as 211
-- built it.
--
-- The Stripe tenant-subscription flow then called ``entitlements.set_comp(account_id, True,
-- reason="Stripe subscription sub_xxx active", actor="stripe-webhook")`` for every tenant that
-- PAYS. A paying customer is the exact OPPOSITE of a write-off. Concretely, that produced:
--   * an invoice line reading "Comped account — Stripe subscription sub_xxx active (waived by
--     stripe-webhook on 2026-08-12)" for a customer who had just paid CA$1500;
--   * ``billing_view.comped_value`` = the full gross, summed by ``saas/admin.py`` into the
--     operator console's "comped" (give-aways) total — collected revenue reported as money
--     given away;
--   * "who is on a freebie?" made unanswerable, because genuine write-offs and paying
--     customers shared one boolean;
--   * a FAKE authorisation (``comp_by='stripe-webhook'``) attached to what is by definition a
--     signed HUMAN decision.
--
-- The effect the webhook wanted was right — do not invoice internally for money Stripe is
-- already collecting — but the CONCEPT was wrong. Two states wanting the same downstream
-- effect ("produce a zero internal payable") is not evidence they are the same state.
--
--   comp      = we CHOSE NOT TO CHARGE.       Human write-off. Needs authoriser + reason
--                                             + optional expiry. Revenue GIVEN AWAY.
--   external  = we ARE CHARGING, elsewhere.   System-recorded fact. Needs the processor and
--                                             the linkage. Revenue COLLECTED, just not by us.
--   neither   = internally invoiced normally.
--
-- An account can be externally billed and NOT comped — that is the ordinary paying tenant, and
-- before this migration it had no representation at all.
--
-- ── PROVENANCE IS HONEST, NOT A FAKE SIGNATURE ───────────────────────────────────────────
-- Comp records WHO AUTHORISED it, because a write-off is a decision. External billing is not a
-- decision — it is an observation about where the money is collected — so there is deliberately
-- NO ``external_billing_by`` column and nothing here pretends a human signed anything. What is
-- recorded is what is actually knowable: the PROCESSOR, the LINKAGE (its subscription id), WHEN
-- the mark was recorded, and WHICH CODE PATH recorded it.
--
--   externally_billed           — the flag the internal biller honours (see
--                                 saas/entitlements._apply_external_billing).
--   external_billing_processor  — WHICH RAIL ('stripe'). Required by the CHECK below: an
--                                 unattributed "somebody else is collecting this" is the same
--                                 silent zero migration 211 exists to prevent.
--   external_billing_ref        — the processor-side linkage (the Stripe subscription id), so
--                                 the zero on the internal invoice is traceable to a real
--                                 collection. Required by the SETTER (400 without it); nullable
--                                 in the DB only so the reclassification below can still
--                                 rescue a historical row whose subscription id is unrecoverable.
--   external_billing_at         — WHEN the mark was recorded.
--   external_billing_source     — the code path that recorded it ('stripe-webhook',
--                                 'operator', 'migration-245'). Provenance, NOT authority.
--
-- There is no ``external_billing_until``: an external arrangement does not lapse on a calendar,
-- it ENDS WHEN THE SUBSCRIPTION ENDS, which arrives as ``customer.subscription.deleted`` and
-- clears the mark. (Comp keeps its expiry precisely because a write-off IS a calendar decision.)
--
-- HISTORY, as with comp, is the append-only ``public.account_ledger``
-- ('external_billing_on' / 'external_billing_off') plus ``public.saas_audit_log``
-- ('account.external_billing' / 'account.external_billing_off'). Neither table is created or
-- constrained here — both already exist (migration 139) and neither constrains ``action``.
-- ⚠️ Those ledger rows carry amount = 0, NOT ``-gross``: nothing is being waived, so nothing
-- may be subtracted from ``ledger_summary.grand_total`` (the figure the Billing view labels as
-- what the account was CHARGED). That is the one deliberate difference from the comp ledger row.
--
-- Read/written with: saas/entitlements.py (external_billing_state / set_external_billing /
-- _apply_external_billing), saas/stripe_webhooks.py (_grant_entitlement / _revoke_entitlement),
-- saas/admin.py (operator billing overview), saas/invoicing.py (clinical lane).
--
-- Control-plane object in public.* ONLY. ``public.accounts`` has NO tenant-schema counterpart —
-- it is not in src/provisioning/templates/user_schema.sql and is never read from a tenant
-- ``search_path`` — so there is deliberately NO template change paired with this migration.
-- Idempotent (ADD COLUMN IF NOT EXISTS + a self-erasing reclassification); safe to re-run.
-- Additive: every existing account reads ``externally_billed = false``, i.e. byte-for-byte
-- today's billing, EXCEPT the accounts the reclassification below repairs.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS externally_billed BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS external_billing_processor TEXT;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS external_billing_ref TEXT;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS external_billing_at TIMESTAMPTZ;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS external_billing_source TEXT;

-- An external-billing mark must always name the RAIL. Enforced in the app (400 without a
-- processor AND without a reference) AND here, so no direct-SQL path can leave an internal
-- invoice silently zeroed with nothing to reconcile it against. Mirrors
-- ``accounts_comp_reason_required`` (migration 211).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_external_billing_processor_required'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_external_billing_processor_required
            CHECK (externally_billed = false
                   OR (external_billing_processor IS NOT NULL
                       AND length(btrim(external_billing_processor)) > 0));
    END IF;
END $$;

-- Fast lookup of the externally-collected roster for the operator billing overview (mirrors
-- idx_accounts_comped).
CREATE INDEX IF NOT EXISTS idx_accounts_externally_billed
    ON public.accounts (externally_billed) WHERE externally_billed;

COMMENT ON COLUMN public.accounts.externally_billed IS
    'EXTERNAL-BILLING flag: this account IS being charged full price, but on an outside '
    'processor''s rail, so the INTERNAL biller stands down and must not invoice it a second '
    'time. NOT a comp — the money is COLLECTED, not waived; the invoice line says so and the '
    'value lands in billing_view.externally_collected_value, never comped_value. Set/cleared '
    'by saas/entitlements.set_external_billing (processor + reference required, ledgered as '
    'external_billing_on/off, audited as account.external_billing).';
COMMENT ON COLUMN public.accounts.external_billing_processor IS
    'WHICH RAIL collects the money (''stripe''). CHECK-required whenever externally_billed.';
COMMENT ON COLUMN public.accounts.external_billing_ref IS
    'Processor-side linkage — the Stripe subscription id. Required by set_external_billing; '
    'NULL only on a row reclassified by migration 245 whose subscription id was unrecoverable.';
COMMENT ON COLUMN public.accounts.external_billing_source IS
    'PROVENANCE of the mark (''stripe-webhook'' | ''operator'' | ''migration-245''): which code '
    'path recorded this system-observed fact. Deliberately NOT an authoriser — unlike a comp, '
    'nobody SIGNS an external-billing mark, because nobody decided it.';

-- ══════════════════════════════════════════════════════════════════════════════════════
-- RECLASSIFY THE MISLABELLED PAYING CUSTOMERS (conservative, exact-match, self-erasing)
-- ══════════════════════════════════════════════════════════════════════════════════════
-- Every account the Stripe flow comped is a paying customer wearing a write-off's clothes. The
-- discriminator is EXACT and machine-written, never a pattern:
--
--     comped = true AND comp_by = 'stripe-webhook'
--
-- ``comp_by`` is written by ``entitlements.set_comp`` as ``actor_user or actor or 'operator'``.
-- Audited against every caller in the tree: the ONLY call site that passes
-- ``actor_user='stripe-webhook'`` is ``stripe_webhooks._grant_entitlement``. The operator route
-- (``admin.set_account_comp``) passes a real operator session identity from ``_operator_actor``.
-- So this predicate cannot select a human comp. It is deliberately EQUALITY on the actor column
-- and NOT an ILIKE/substring on ``comp_reason`` — a reason string is user-authored text and a
-- pattern over it could match a genuine comp whose reason merely MENTIONS Stripe.
--
-- The subscription id is recovered RELATIONALLY from the snapshot table (no string parsing of
-- ``comp_reason``). If no live snapshot row exists the account is still reclassified with a NULL
-- reference — losing the linkage is far better than leaving a paying customer flagged as a
-- give-away, and the CHECK above only requires the processor.
--
-- SELF-ERASING = IDEMPOTENT: the statement clears ``comp_by``, so a second run selects nothing.
--
-- ⚠️ WHAT THIS DOES NOT DO, ON PURPOSE: it does not touch ``public.account_ledger``. Those
-- historical 'comp' rows carry ``amount = -gross`` and are APPEND-ONLY by design, so the
-- distortion they introduced into ``ledger_summary.grand_total`` cannot be rewritten here and
-- must be reconciled by an operator. Review query for exactly those rows:
--
--     SELECT l.account_id, a.name, l.ts, l.action, l.amount, l.note
--       FROM public.account_ledger l JOIN public.accounts a USING (account_id)
--      WHERE l.actor_user = 'stripe-webhook' AND l.action IN ('comp', 'comp_off')
--      ORDER BY l.account_id, l.ts;
--
-- And a review query for any comp this migration deliberately left alone, so an operator can
-- eyeball whether a human comp was ever hand-written over a Stripe payer:
--
--     SELECT account_id, name, comp_by, comp_at, comp_until, comp_reason
--       FROM public.accounts WHERE comped = true ORDER BY comp_at;
DO $$
DECLARE
    n_fixed  INTEGER := 0;
    n_noref  INTEGER := 0;
BEGIN
    WITH recovered AS (
        SELECT a.account_id,
               (SELECT s.subscription_id
                  FROM public.stripe_subscriptions s
                 WHERE s.account_id = a.account_id
                   AND s.status IN ('active', 'trialing', 'past_due', 'unpaid', 'incomplete')
                 ORDER BY s.updated_at DESC
                 LIMIT 1) AS sub_id
          FROM public.accounts a
         WHERE a.comped = true
           AND a.comp_by = 'stripe-webhook'
    ), fixed AS (
        UPDATE public.accounts a
           SET externally_billed          = true,
               external_billing_processor = 'stripe',
               external_billing_ref       = r.sub_id,
               external_billing_at        = COALESCE(a.comp_at, NOW()),
               external_billing_source    = 'migration-245',
               -- The comp was never a comp. Clear it wholesale so the give-away roster tells
               -- the truth again; the columns are re-settable by an operator at any time.
               comped      = false,
               comp_reason = NULL,
               comp_by     = NULL,
               comp_at     = NULL,
               comp_until  = NULL,
               updated_at  = NOW()
          FROM recovered r
         WHERE a.account_id = r.account_id
        RETURNING a.account_id, a.external_billing_ref
    )
    SELECT count(*)::int, count(*) FILTER (WHERE external_billing_ref IS NULL)::int
      INTO n_fixed, n_noref
      FROM fixed;
    IF n_fixed > 0 THEN
        RAISE NOTICE 'migration 245: reclassified % stripe-webhook comp(s) as externally billed '
                     '(% without a recoverable subscription id) — these were PAYING customers '
                     'mislabelled as give-aways; the historical account_ledger comp rows are '
                     'append-only and were NOT touched (see the review query in this file)',
                     n_fixed, n_noref;
    END IF;
END $$;
