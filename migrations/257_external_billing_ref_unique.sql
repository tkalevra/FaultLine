-- Migration 257: ONE SUBSCRIPTION, ONE ACCOUNT — a UNIQUE index on
--                public.accounts.external_billing_ref  (CLOSED-SOURCE SaaS layer)
-- Date: 2026-08-13
--
-- ── WHY: THE SAME MONEY COUNTED TWICE ────────────────────────────────────────────────────
--
-- Migration 245 added ``external_billing_ref`` — the processor-side linkage (the Stripe
-- subscription id) that makes the zero on an internal invoice traceable to a real collection.
-- It added NO uniqueness. ``entitlements.external_collected_monthly`` resolves what the external
-- rail ACTUALLY collects by joining
--
--     public.signed_contracts.stripe_subscription_id = public.accounts.external_billing_ref
--
-- on EXACT equality, and the resulting figure is CREDITED against that account's internal bill.
-- So if two accounts carry the SAME ``external_billing_ref``, both resolve the SAME collected
-- amount and both credit it: one Stripe collection of CA$1,500 wipes CA$3,000 of internal
-- payable, and each invoice states — precisely, in the wording migration 245's repair
-- introduced — that the money was "charged in full on that subscription". Two customers each
-- shown an affirmatively FALSE statement, and the difference is silently unbilled.
--
-- The column is written by ``entitlements.set_external_billing`` (the Stripe webhook path) and
-- by 245's own reclassification of the machine-comp rows. Neither can see the other's rows, and
-- nothing in the application checks for a collision. A redelivered/duplicated subscription id,
-- a hand-repaired row, or a future second webhook path is all it takes.
--
-- DIRECTION OF THE ERROR IS UNDER-BILLING, which is why this is a constraint and not a report.
--
-- ── SCOPE, DELIBERATELY NARROW ───────────────────────────────────────────────────────────
-- PARTIAL index (``WHERE external_billing_ref IS NOT NULL``). The overwhelming majority of
-- accounts have no external rail at all, and in PostgreSQL NULLs are distinct under a unique
-- index anyway (SQL:2016 §11.7 / PostgreSQL "Unique Indexes": "null values are not considered
-- equal") — the predicate makes that intent explicit AND keeps the index small. It also leaves
-- 245's nullable-for-reclassification behaviour completely untouched.
--
-- NOT ``CREATE INDEX CONCURRENTLY``, for the reason migration 231 already wrote down and which
-- applies verbatim here: the migration runner applies each file INSIDE an explicit transaction
-- (``saas/migration_runner.py::_apply_one`` sets ``conn.autocommit = False``, executes the SQL
-- and commits the DDL together with the ledger row), and CONCURRENTLY cannot run inside a
-- transaction OR inside a DO block — so it could not be gated on the pre-check below. Worse, a
-- failed CONCURRENTLY leaves an INVALID index behind that ``IF NOT EXISTS`` then SILENTLY SKIPS
-- on the next boot: a clean-looking migration run with uniqueness never enforced. This table is
-- an account roster (production holds 4 rows); there is no lock-duration problem to solve.
--
-- ── FAILURE MODE: REFUSE AND NAME THE DUPLICATES ─────────────────────────────────────────
-- A blind ``CREATE UNIQUE INDEX`` on a table that already holds a duplicate aborts with
-- PostgreSQL's own message ("could not create unique index … Key (external_billing_ref)=(…) is
-- duplicated"), which names ONE key and leaves the operator to go find the accounts. This
-- migration pre-checks, and if it finds a collision it RAISES with every duplicated reference
-- and the account ids holding it, and changes NOTHING. A migration must not pick which of two
-- real accounts owns a subscription — that is a money decision and it is unrecoverable.
--
-- Verified before writing this: production holds 4 accounts and 0 rows in
-- ``public.stripe_subscriptions``, so a duplicate is very unlikely THERE. The guard exists for
-- every other deployment, and for the re-run case.
--
-- Idempotent and re-runnable: the pre-check is a read, and the index is IF NOT EXISTS.

DO $$
DECLARE
    _groups INTEGER;
    _rows   INTEGER;
    _sample TEXT;
BEGIN
    IF to_regclass('public.accounts') IS NULL THEN
        RAISE NOTICE 'migration 257: public.accounts does not exist here — nothing to do.';
        RETURN;
    END IF;

    -- Column absent = migration 245 has not run against this database yet. Say so and stop;
    -- do NOT create the column (245 owns its definition, its CHECK and its comments).
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'accounts'
                      AND column_name = 'external_billing_ref') THEN
        RAISE NOTICE 'migration 257: public.accounts.external_billing_ref is absent '
                     '(migration 245 has not been applied here) — nothing to do.';
        RETURN;
    END IF;

    SELECT count(*), COALESCE(sum(d.n), 0) INTO _groups, _rows
      FROM (SELECT count(*) AS n
              FROM public.accounts
             WHERE external_billing_ref IS NOT NULL
             GROUP BY external_billing_ref
            HAVING count(*) > 1) d;

    IF _groups > 0 THEN
        SELECT string_agg(t.external_billing_ref || ' (' || t.n || ' accounts: ' || t.ids || ')',
                          E'\n    ' ORDER BY t.n DESC, t.external_billing_ref)
          INTO _sample
          FROM (SELECT external_billing_ref,
                       count(*) AS n,
                       string_agg(account_id::text, ', ' ORDER BY created_at) AS ids
                  FROM public.accounts
                 WHERE external_billing_ref IS NOT NULL
                 GROUP BY external_billing_ref
                HAVING count(*) > 1
                 ORDER BY count(*) DESC, external_billing_ref
                 LIMIT 10) t;
        RAISE EXCEPTION
            'migration 257: public.accounts already holds % external billing reference(s) '
            'shared by % accounts. NOTHING has been created, changed or deleted. Each of those '
            'accounts currently credits the SAME collected amount against its own internal '
            'invoice, so the money is counted once per account.%    Duplicates (first 10):%    %',
            _groups, _rows, E'\n', E'\n', _sample
            USING HINT = 'Decide which account genuinely owns each subscription and stand the '
                         'external mark down on the others '
                         '(entitlements.set_external_billing(account_id, False), or an '
                         'operator-console correction), then re-run this migration. A migration '
                         'must not pick which of two real accounts owns a subscription — that '
                         'is a money decision, and it is unrecoverable.';
    END IF;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_external_billing_ref
        ON public.accounts (external_billing_ref)
        WHERE external_billing_ref IS NOT NULL;

    -- Inside the block: the two RETURNs above leave the index absent, and a top-level
    -- COMMENT ON INDEX would then abort a migration that correctly decided to do nothing.
    COMMENT ON INDEX public.uq_accounts_external_billing_ref IS
        'One external subscription belongs to ONE account. Without it, two accounts sharing an '
        'external_billing_ref each credit the same collected amount against their own internal '
        'invoice (entitlements.external_collected_monthly joins signed_contracts on exact '
        'equality) — the same money counted twice, under-billing on both. Partial: NULL = no '
        'external rail, unconstrained. Added by migration 257.';

    RAISE NOTICE 'migration 257: uq_accounts_external_billing_ref in place '
                 '(partial UNIQUE on public.accounts.external_billing_ref).';
END $$;
