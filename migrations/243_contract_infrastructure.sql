-- 243_contract_infrastructure.sql — Terms of Service templates + signed contract audit trail
--
-- PURPOSE: a self-serve tenant subscription path where the customer READS, ACCEPTS, and PAYS —
-- with the signed terms maintained as a permanent financial record. Required for tenant-level
-- sign-ups where the commitment is $750/mo (Team) or $1500/mo (Enterprise) — financial obligations
-- that demand a clear, recorded agreement (not just a card charge).
--
-- Two tables:
--   contract_templates — the VERSIONED ToS text. One active version at a time; the operator
--                        publishes a new version to update terms. Each version is immutable once
--                        published (retired_at is set by the NEXT version, never edited).
--   signed_contracts   — the APPEND-ONLY audit trail. Each row = one customer acceptance of one
--                        version. Carries the signer's identity (email, IP, user-agent — the
--                        electronic-signature facts under PIPEDA/ESIGN), the tier they subscribed
--                        to, the AGREED PRICE, the Stripe price/session/subscription IDs for
--                        linkage, AND a full SNAPSHOT of the terms text at acceptance time (so the
--                        exact words they agreed to are preserved even if the template is revised).
--
-- ─────────────────────────────────────────────────────────────────────────────────────
-- ⚠️ CORRECTED 2026-08-12 — the first cut of this migration was DEAD ON ARRIVAL. Two defects:
--
--   (1) `retired_at TIMESTAPZTZ` — a typo for TIMESTAMPTZ. The migration could not even parse,
--       so NOTHING in this file had ever run.
--
--   (2) THE LOAD-BEARING ONE: the append-only trigger was a blanket `RAISE EXCEPTION` on
--       BEFORE UPDATE OR DELETE, and `advance_signed_contract_status` was documented as bypassing
--       it because it is SECURITY DEFINER — *"the append-only trigger does NOT fire on SECURITY
--       DEFINER functions (they run as the owner, bypassing triggers)"*. *** THAT IS FALSE. ***
--       SECURITY DEFINER changes the PRIVILEGE context of a function; it has no effect whatsoever
--       on whether triggers fire. Triggers fire on the UPDATE regardless of who is executing it.
--       Measured on PostgreSQL 16 before writing this correction: calling the SECURITY DEFINER
--       advance() against a table with a blanket append-only trigger returns
--           ERROR: append-only
--           CONTEXT: PL/pgSQL function blocker() line 2 at RAISE
--           SQL statement "UPDATE ... SET status='completed' WHERE id=p_id"
--       and the row's status stays 'accepted'. The ENTIRE contract-completion lifecycle — the
--       webhook linking the Stripe subscription and marking the contract fulfilled — could never
--       have worked. (Only ALTER TABLE ... DISABLE TRIGGER or session_replication_role='replica'
--       actually suppress a trigger, and neither is appropriate here.)
--
--   THE FIX is the pattern migration 235 (public.stripe_events) already proved in this codebase:
--   a COLUMN-AWARE trigger. DELETE is always blocked. UPDATE is permitted ONLY when every
--   acceptance fact is byte-identical; the narrow lifecycle-linkage columns are allowed through,
--   and are themselves WRITE-ONCE (NULL → value is allowed; value → different value is refused),
--   so a linked subscription can never be silently re-pointed at another contract. This gives a
--   stronger guarantee than the original design *and* it actually runs.
-- ─────────────────────────────────────────────────────────────────────────────────────

-- ── contract_templates: the versioned ToS text ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.contract_templates (
    id           BIGSERIAL PRIMARY KEY,
    version      TEXT NOT NULL UNIQUE,        -- e.g. "2026-08-12-v1"; operator-chosen, must be unique
    title        TEXT NOT NULL,               -- e.g. "FaultLine Terms of Service"
    body_markdown TEXT NOT NULL,              -- the full terms text (markdown — rendered on display)
    effective_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- when this version became active
    retired_at   TIMESTAMPTZ,                 -- NULL = active; set when superseded (never edited directly)
    created_by   TEXT,                        -- operator who published it
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contract_templates_effective
    ON public.contract_templates (effective_at DESC);

-- Exactly ONE active version at a time — enforced, not merely intended. publish_terms_version()
-- retires the incumbent in the same transaction as the insert; this index makes a second active
-- row impossible even if a future code path forgets.
CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_templates_one_active
    ON public.contract_templates ((retired_at IS NULL)) WHERE retired_at IS NULL;

-- ── signed_contracts: the append-only acceptance audit trail ──────────────────────
CREATE TABLE IF NOT EXISTS public.signed_contracts (
    id                       BIGSERIAL PRIMARY KEY,
    contract_template_id     BIGINT NOT NULL REFERENCES public.contract_templates(id),
    -- the SIGNER (electronic-signature facts under PIPEDA/ESIGN)
    signer_email             TEXT NOT NULL,
    signer_user_id           UUID,             -- the individual user id (if known at signup)
    signer_ip                TEXT,
    signer_user_agent        TEXT,
    signer_name              TEXT,             -- the legal name typed at acceptance (the "signature")
    -- the SUBSCRIPTION context
    account_id               UUID,             -- the tenant account (filled on provisioning)
    tier                     TEXT NOT NULL,    -- 'team' | 'enterprise' | 'individual'
    stripe_price_id          TEXT,             -- the Stripe Price they subscribed at
    stripe_checkout_session_id TEXT,           -- the Checkout Session (linked immediately after acceptance)
    stripe_subscription_id   TEXT,             -- filled when the webhook confirms the subscription
    -- the AGREED PRICE, recorded in our own words. A Stripe price_id is an opaque pointer that
    -- could in principle be re-pointed on Stripe's side; a financial record must state the amount
    -- the customer actually agreed to pay, in the currency and interval they agreed to pay it.
    agreed_amount_cents      INTEGER,          -- e.g. 75000 = CA$750.00
    agreed_currency          TEXT NOT NULL DEFAULT 'CAD',
    agreed_interval          TEXT NOT NULL DEFAULT 'month',
    -- the RECORD (immutable)
    contract_snapshot        TEXT NOT NULL,    -- the EXACT terms text at acceptance time
    contract_version         TEXT,             -- the template version string at acceptance time
    accepted_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status                   TEXT NOT NULL DEFAULT 'accepted'  -- accepted | completed | voided
        CONSTRAINT signed_contracts_status_valid
        CHECK (status IN ('accepted', 'completed', 'voided'))
);

-- Migrations are re-runnable against a schema created by an earlier cut of this file.
ALTER TABLE public.signed_contracts ADD COLUMN IF NOT EXISTS agreed_amount_cents INTEGER;
ALTER TABLE public.signed_contracts ADD COLUMN IF NOT EXISTS agreed_currency TEXT NOT NULL DEFAULT 'CAD';
ALTER TABLE public.signed_contracts ADD COLUMN IF NOT EXISTS agreed_interval TEXT NOT NULL DEFAULT 'month';
ALTER TABLE public.signed_contracts ADD COLUMN IF NOT EXISTS contract_version TEXT;

CREATE INDEX IF NOT EXISTS idx_signed_contracts_email
    ON public.signed_contracts (signer_email);
CREATE INDEX IF NOT EXISTS idx_signed_contracts_session
    ON public.signed_contracts (stripe_checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_signed_contracts_account
    ON public.signed_contracts (account_id);
CREATE INDEX IF NOT EXISTS idx_signed_contracts_subscription
    ON public.signed_contracts (stripe_subscription_id);

-- One Checkout Session belongs to exactly one signed contract. Without this a webhook retry or a
-- duplicated POST could produce two contracts claiming the same session, and the "which contract
-- does this payment satisfy" question would have no single answer.
CREATE UNIQUE INDEX IF NOT EXISTS idx_signed_contracts_session_unique
    ON public.signed_contracts (stripe_checkout_session_id)
    WHERE stripe_checkout_session_id IS NOT NULL;

-- ── APPEND-ONLY enforcement on the acceptance facts ───────────────────────────────
-- Same shape as public.stripe_events (migration 235), which is the proven pattern in this
-- codebase. DELETE is always blocked. On UPDATE every acceptance fact must be byte-identical;
-- only the narrow lifecycle-linkage columns may change, and those are WRITE-ONCE.
--
-- What this protects: the signer facts (who, when, from where, under what name), the exact terms
-- text they were shown, and the price they agreed to. Those are the contract. They can never be
-- rewritten by any application code path — including the advance function below.
-- (A privileged DBA can still drop the trigger; that is out of scope for app-layer tamper-evidence.)
CREATE OR REPLACE FUNCTION public.signed_contracts_immutable_columns()
RETURNS trigger AS $$
DECLARE
    bad TEXT;
BEGIN
    -- TRUNCATE is always blocked. This branch is reached from the STATEMENT-level triggers
    -- declared below; a statement trigger has no NEW/OLD, so it must return before any column
    -- comparison. TG_TABLE_NAME is used because the same function guards both tables.
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'public.% is append-only — TRUNCATE is not permitted. These rows are '
                        'permanent financial records.', TG_TABLE_NAME;
    END IF;

    -- DELETE is always blocked (a signed contract is a permanent financial record).
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'public.signed_contracts is append-only — DELETE is not permitted. '
                        'A signed contract is a permanent financial record.';
    END IF;

    -- ACCEPTANCE FACTS — immutable forever.
    IF NEW.id                     IS DISTINCT FROM OLD.id                     THEN bad := 'id';                     END IF;
    IF NEW.contract_template_id   IS DISTINCT FROM OLD.contract_template_id   AND bad IS NULL THEN bad := 'contract_template_id';   END IF;
    IF NEW.signer_email           IS DISTINCT FROM OLD.signer_email           AND bad IS NULL THEN bad := 'signer_email';           END IF;
    IF NEW.signer_user_id         IS DISTINCT FROM OLD.signer_user_id         AND bad IS NULL THEN bad := 'signer_user_id';         END IF;
    IF NEW.signer_ip              IS DISTINCT FROM OLD.signer_ip              AND bad IS NULL THEN bad := 'signer_ip';              END IF;
    IF NEW.signer_user_agent      IS DISTINCT FROM OLD.signer_user_agent      AND bad IS NULL THEN bad := 'signer_user_agent';      END IF;
    IF NEW.signer_name            IS DISTINCT FROM OLD.signer_name            AND bad IS NULL THEN bad := 'signer_name';            END IF;
    IF NEW.tier                   IS DISTINCT FROM OLD.tier                   AND bad IS NULL THEN bad := 'tier';                   END IF;
    IF NEW.stripe_price_id        IS DISTINCT FROM OLD.stripe_price_id        AND bad IS NULL THEN bad := 'stripe_price_id';        END IF;
    IF NEW.agreed_amount_cents    IS DISTINCT FROM OLD.agreed_amount_cents    AND bad IS NULL THEN bad := 'agreed_amount_cents';    END IF;
    IF NEW.agreed_currency        IS DISTINCT FROM OLD.agreed_currency        AND bad IS NULL THEN bad := 'agreed_currency';        END IF;
    IF NEW.agreed_interval        IS DISTINCT FROM OLD.agreed_interval        AND bad IS NULL THEN bad := 'agreed_interval';        END IF;
    IF NEW.contract_snapshot      IS DISTINCT FROM OLD.contract_snapshot      AND bad IS NULL THEN bad := 'contract_snapshot';      END IF;
    IF NEW.contract_version       IS DISTINCT FROM OLD.contract_version       AND bad IS NULL THEN bad := 'contract_version';       END IF;
    IF NEW.accepted_at            IS DISTINCT FROM OLD.accepted_at            AND bad IS NULL THEN bad := 'accepted_at';            END IF;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'public.signed_contracts.% is an acceptance fact — it is append-only and '
                        'cannot be modified. Only status, stripe_checkout_session_id, '
                        'stripe_subscription_id and account_id may advance.', bad;
    END IF;

    -- LIFECYCLE LINKAGE — WRITE-ONCE. NULL → value is the linkage moment; value → different value
    -- would silently re-point a signed contract at someone else's payment.
    IF OLD.stripe_checkout_session_id IS NOT NULL
       AND NEW.stripe_checkout_session_id IS DISTINCT FROM OLD.stripe_checkout_session_id THEN
        RAISE EXCEPTION 'public.signed_contracts.stripe_checkout_session_id is write-once '
                        '(already linked to %) — it cannot be re-pointed.',
                        OLD.stripe_checkout_session_id;
    END IF;
    IF OLD.stripe_subscription_id IS NOT NULL
       AND NEW.stripe_subscription_id IS DISTINCT FROM OLD.stripe_subscription_id THEN
        RAISE EXCEPTION 'public.signed_contracts.stripe_subscription_id is write-once '
                        '(already linked to %) — it cannot be re-pointed.',
                        OLD.stripe_subscription_id;
    END IF;
    IF OLD.account_id IS NOT NULL AND NEW.account_id IS DISTINCT FROM OLD.account_id THEN
        RAISE EXCEPTION 'public.signed_contracts.account_id is write-once (already linked to %) '
                        '— it cannot be re-pointed.', OLD.account_id;
    END IF;

    -- STATUS — forward-only. A completed (paid, fulfilled) contract cannot be walked back to
    -- 'accepted'; that would erase the evidence that the subscription was ever created.
    IF OLD.status = 'completed' AND NEW.status = 'accepted' THEN
        RAISE EXCEPTION 'public.signed_contracts.status cannot regress from completed to accepted.';
    END IF;
    IF OLD.status = 'voided' AND NEW.status <> 'voided' THEN
        RAISE EXCEPTION 'public.signed_contracts.status cannot advance out of voided.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_signed_contracts_immutable ON public.signed_contracts;
CREATE TRIGGER trg_signed_contracts_immutable
    BEFORE UPDATE OR DELETE ON public.signed_contracts
    FOR EACH ROW EXECUTE FUNCTION public.signed_contracts_immutable_columns();

-- ── TRUNCATE: a SECOND trigger, because the first one cannot see it ────────────────
-- ⚠️ A ROW-LEVEL TRIGGER NEVER FIRES ON TRUNCATE. This is not an edge case and it is not a
-- "privileged DBA" scenario — TRUNCATE is one ordinary statement, the app role owns this table
-- and holds the TRUNCATE privilege, and the trigger above is powerless against it. Measured on
-- PostgreSQL 16 with trg_signed_contracts_immutable provably ENABLED (tgenabled='O'):
--     SELECT count(*) FROM signed_contracts;  ->  1
--     TRUNCATE signed_contracts;              ->  (no error)
--     SELECT count(*) FROM signed_contracts;  ->  0
-- The entire financial record was erased silently, while the customer-facing page states the
-- record "cannot be edited or deleted afterwards — not by you, and not by us."
-- PostgreSQL's CREATE TRIGGER docs are explicit that TRUNCATE triggers must be FOR EACH STATEMENT
-- (a row trigger on TRUNCATE is rejected outright), which is exactly why the protection has to be
-- a separate trigger rather than another event on the one above.
DROP TRIGGER IF EXISTS trg_signed_contracts_no_truncate ON public.signed_contracts;
CREATE TRIGGER trg_signed_contracts_no_truncate
    BEFORE TRUNCATE ON public.signed_contracts
    FOR EACH STATEMENT EXECUTE FUNCTION public.signed_contracts_immutable_columns();

-- Same protection for the templates: a signed row references its template by FK, and the audit
-- trail is only meaningful if the version it points at still exists.
DROP TRIGGER IF EXISTS trg_contract_templates_no_truncate ON public.contract_templates;
CREATE TRIGGER trg_contract_templates_no_truncate
    BEFORE TRUNCATE ON public.contract_templates
    FOR EACH STATEMENT EXECUTE FUNCTION public.signed_contracts_immutable_columns();

-- ── contract_templates: PUBLISHED TEXT IS IMMUTABLE ───────────────────────────────
-- ⚠️ ADDED after a second review pass found the templates table had TRUNCATE protection and
-- nothing else. Measured on the live schema before this trigger existed:
--     UPDATE contract_templates SET body_markdown='## HOSTILE REWRITE…';  -> UPDATE 1
--     DELETE FROM contract_templates WHERE id=<unreferenced>;             -> DELETE 1
-- The signed row's own `contract_snapshot` still preserved the evidence, so this was never
-- audit-FATAL — but a signed contract CITES its version by id and version string, and a citation
-- to a mutable document is not a citation. `publish_terms_version`'s own contract is that a
-- version is immutable once published; that was policy in Python with nothing enforcing it.
--
-- `retired_at` is the ONE mutable column: publishing a new version retires the incumbent, which
-- is a lifecycle transition, not an edit to the text. It is write-once (a retired version can
-- never be un-retired) so a superseded version cannot be quietly brought back into force.
CREATE OR REPLACE FUNCTION public.contract_templates_immutable_columns()
RETURNS trigger AS $$
DECLARE
    bad TEXT;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'public.contract_templates is append-only — TRUNCATE is not permitted.';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'public.contract_templates is append-only — DELETE is not permitted. '
                        'Signed contracts cite these versions; retire a version instead.';
    END IF;
    IF NEW.id            IS DISTINCT FROM OLD.id            THEN bad := 'id';            END IF;
    IF NEW.version       IS DISTINCT FROM OLD.version       AND bad IS NULL THEN bad := 'version';       END IF;
    IF NEW.title         IS DISTINCT FROM OLD.title         AND bad IS NULL THEN bad := 'title';         END IF;
    IF NEW.body_markdown IS DISTINCT FROM OLD.body_markdown AND bad IS NULL THEN bad := 'body_markdown'; END IF;
    IF NEW.effective_at  IS DISTINCT FROM OLD.effective_at  AND bad IS NULL THEN bad := 'effective_at';  END IF;
    IF NEW.created_by    IS DISTINCT FROM OLD.created_by    AND bad IS NULL THEN bad := 'created_by';    END IF;
    IF NEW.created_at    IS DISTINCT FROM OLD.created_at    AND bad IS NULL THEN bad := 'created_at';    END IF;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'public.contract_templates.% is immutable once published — publish a NEW '
                        'version instead of editing this one. Customers have signed this text.', bad;
    END IF;
    IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
        RAISE EXCEPTION 'public.contract_templates.retired_at is write-once — a retired version '
                        'cannot be un-retired or re-dated.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_contract_templates_immutable ON public.contract_templates;
CREATE TRIGGER trg_contract_templates_immutable
    BEFORE UPDATE OR DELETE ON public.contract_templates
    FOR EACH ROW EXECUTE FUNCTION public.contract_templates_immutable_columns();

-- ── the ONE sanctioned lifecycle-advance path ─────────────────────────────────────
-- This function is the only place in the codebase that UPDATEs signed_contracts. It touches
-- ONLY status + the three write-once linkage columns, so the column-aware trigger above lets it
-- through; any attempt to widen it to an acceptance fact will be refused by the trigger at
-- runtime, not merely by convention.
--
-- SECURITY DEFINER is retained so the lifecycle can advance under a least-privileged app role,
-- and `SET search_path` is PINNED: an unpinned SECURITY DEFINER function is a privilege-escalation
-- vector (a caller controls search_path and can shadow an unqualified name), and this codebase
-- rebinds search_path per tenant on every request, so the risk is live rather than theoretical.
-- (PostgreSQL docs, "Writing SECURITY DEFINER Functions Safely".)
CREATE OR REPLACE FUNCTION public.advance_signed_contract_status(
    p_id BIGINT,
    p_status TEXT,
    p_stripe_subscription_id TEXT DEFAULT NULL,
    p_account_id UUID DEFAULT NULL,
    p_stripe_checkout_session_id TEXT DEFAULT NULL
) RETURNS BOOLEAN AS $$
DECLARE
    updated_count INTEGER;
    cur RECORD;
BEGIN
    IF p_status NOT IN ('accepted', 'completed', 'voided') THEN
        RAISE EXCEPTION 'invalid signed-contract status: %', p_status;
    END IF;

    -- ⚠️ WHY THIS CONFLICT CHECK EXISTS — the COALESCE below is a TRAP without it.
    -- COALESCE(col, p_arg) keeps the incumbent when the column is already set. That is the
    -- correct DATA behaviour (write-once), but it makes the column UNCHANGED, so NEW = OLD and
    -- the write-once branch of the append-only trigger NEVER FIRES. The UPDATE still MATCHES the
    -- row, so ROW_COUNT is 1 and the function used to RETURN TRUE. Measured before this fix, on a
    -- row already linked to sub_test_xyz789:
    --     SELECT advance_signed_contract_status(1,'completed','sub_ATTACKER',...);  ->  t
    --     SELECT stripe_subscription_id ...;                        ->  sub_test_xyz789 (unchanged)
    -- The data was safe; the ANSWER was a lie. A caller that trusts the boolean would conclude the
    -- re-link succeeded. The guard and the COALESCE cancelled each other out, and each read as
    -- correct in isolation — which is exactly why neither the trigger nor the function looked wrong.
    -- On a money path a boolean that cannot distinguish "did it" from "declined to do it" is not a
    -- usable return value, so a conflicting request is now a clean FALSE.
    SELECT stripe_subscription_id, account_id, stripe_checkout_session_id
      INTO cur
      FROM public.signed_contracts WHERE id = p_id;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    IF (p_stripe_subscription_id IS NOT NULL
        AND cur.stripe_subscription_id IS NOT NULL
        AND cur.stripe_subscription_id <> p_stripe_subscription_id)
    OR (p_account_id IS NOT NULL
        AND cur.account_id IS NOT NULL
        AND cur.account_id <> p_account_id)
    OR (p_stripe_checkout_session_id IS NOT NULL
        AND cur.stripe_checkout_session_id IS NOT NULL
        AND cur.stripe_checkout_session_id <> p_stripe_checkout_session_id)
    THEN
        RAISE WARNING 'advance_signed_contract_status: refusing to re-point contract % '
                      '(subscription %/%, account %/%, session %/%)',
                      p_id, cur.stripe_subscription_id, p_stripe_subscription_id,
                      cur.account_id, p_account_id,
                      cur.stripe_checkout_session_id, p_stripe_checkout_session_id;
        RETURN FALSE;
    END IF;

    UPDATE public.signed_contracts
    SET status = p_status,
        stripe_subscription_id     = COALESCE(stripe_subscription_id, p_stripe_subscription_id),
        account_id                 = COALESCE(account_id, p_account_id),
        stripe_checkout_session_id = COALESCE(stripe_checkout_session_id, p_stripe_checkout_session_id)
    WHERE id = p_id AND status IN ('accepted', 'completed');
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count > 0;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;

COMMENT ON TABLE public.signed_contracts IS
    'Append-only audit trail of customer ToS acceptances. Each row is a permanent financial '
    'record carrying the electronic-signature facts (email, IP, user-agent, typed name, '
    'timestamp), the exact terms text at acceptance time, and the agreed price. Linked to the '
    'Stripe subscription for the full billing trail. Acceptance facts are immutable (BEFORE '
    'UPDATE trigger); status + the three linkage columns are the only mutable fields, and the '
    'linkage columns are write-once. Under PIPEDA (Canada) + ESIGN (US), these facts constitute '
    'a legally valid electronic signature.';

COMMENT ON TABLE public.contract_templates IS
    'Versioned Terms of Service text. One active version at a time (retired_at IS NULL, enforced '
    'by a partial unique index). The operator publishes new versions; each signed_contracts row '
    'references the version the customer agreed to + carries a full snapshot of the text at '
    'acceptance time.';

-- ── seed the FIRST terms version (operator can publish a new one from the console) ─
-- Dollar-quoted so the markdown can contain apostrophes and quotes without escaping games.
-- Drafting notes for whoever revises this:
--   * Every commitment is SPECIFIC and checkable. No "at our discretion", no "reasonable
--     efforts", no "standard retention period" — those are the hand-waving that makes a consumer
--     contract unenforceable and unfair in equal measure.
--   * The price is NOT stated in the body: one terms version serves both Team and Enterprise, and
--     the amount the customer actually agreed to is recorded per-signature in
--     signed_contracts.agreed_amount_cents / tier / stripe_price_id, and shown on the page above
--     the signature box. The body governs the MECHANICS of billing; the row records the AMOUNT.
--   * ⚠️ REVIEW BEFORE LIVE: this is a competently-drafted commercial SaaS agreement, not legal
--     advice. Have Ontario counsel review it before the first paying tenant signs.
INSERT INTO public.contract_templates (version, title, body_markdown, effective_at, created_by)
VALUES (
    '2026-08-12-v1',
    'FaultLine Subscription Agreement',
    $terms$## FaultLine Subscription Agreement

**Effective date of this version:** August 12, 2026

This agreement is between you (the "Customer") and Volenti ("we", "us"), the operator of
FaultLine. It takes effect when you accept it and it governs your use of the service for as long
as your subscription is active.

### 1. What the service is

FaultLine is a write-validated memory service for AI applications. You send it information; it
extracts, validates, and stores that information as structured facts in a database, and returns
those facts when your applications ask for them.

We provide the service as a hosted application. We do not guarantee that the service will be
uninterrupted or error-free, and this agreement does not itself set an uptime percentage or
service credits.

You never have to talk to us to buy or use FaultLine — every plan is self-serve, and nothing on
this page is gated behind a sales conversation. If you want a contractual uptime commitment, write
to legal@volenti.ca and we will agree one with you in a separate written service-level agreement;
where such an agreement exists, it governs uptime and prevails over this section. You can do that
before or after you subscribe — subscribing first does not weaken your position.

### 2. Your account and your users

You choose a plan with a number of seats. Each seat is one named person or one automated agent.
You may ADD seats from your console at any time; an added seat is charged from the invoice after
you add it. You may also REMOVE a seat at any time to free it for someone else — but a seat you
have provisioned is committed for the remainder of your term (section 3), so removing it stops
future use, not the charge, and there is no refund for the balance of the term. Your console
states this at the moment you remove a seat.

You are responsible for who you give access to, and for what they do with it. Keep credentials
secure and tell us promptly at security@volenti.ca if you believe an account has been compromised.

### 3. Fees, billing, and cancellation

- **What you pay.** The plan and monthly fee shown on the checkout page at the moment you accept
  this agreement. That amount, the currency, and the billing interval are recorded with your
  signature and are shown on your receipt.
- **When you pay.** Monthly, in advance, on the day of the month you first subscribed, charged to
  the payment method you provide. Payments are processed by Stripe; we never see or store your
  full card number.
- **Taxes.** Fees are exclusive of applicable taxes. Canadian sales tax is calculated and added at
  checkout based on your billing address.
- **Your commitment.** This is a **{{CONTRACT_TERM_MONTHS}}-month term**, starting on the day you subscribe. You are
  committing to the plan for that period. We bill it in monthly instalments rather than asking
  for the whole term up front — the monthly charge is how you PAY, not how long you are committed.
  Please be sure this is what you want before you sign: it is the one part of this agreement you
  cannot get out of early.
- **Renewal.** At the end of the term your subscription renews for a further {{CONTRACT_TERM_MONTHS}}-month term unless
  you tell us otherwise. To stop it renewing, email billing@volenti.ca at any time before the term
  ends; you keep the service to the end of the term you have paid for and are not charged again.
- **Cancelling during a term.** You may stop using the service at any time and we will confirm
  within one business day, but the monthly instalments for the remainder of the term remain
  payable. We do not refund the balance of a term.
- **Refunds.** We do not refund partial months or the unused balance of a term. There are two
  exceptions, and in both we refund every month of the term you have paid for and not used,
  calculated by the day: if we make a material breaking change to the service that you do not
  accept, or if we terminate your account for a reason other than your breach of section 5.
- **Individual accounts are different.** If you are a single user on an individual plan rather
  than a team or company account, there is NO term and no commitment: you are month to month,
  your first month is free, and you can stop at any time and pay nothing further. This section's
  {{CONTRACT_TERM_MONTHS}}-month term applies only to team and company accounts.
- **Failed payments.** If a payment fails, our payment processor retries it on its own schedule
  and emails you at the address on your Stripe receipt. If the payment is still unpaid after those
  retries are exhausted, we may suspend the account until it is settled. Suspension pauses access;
  it does not delete your data. We delete data only under section 7. If we suspend an account for
  non-payment we will tell you at the address on your account, and you can restore service by
  settling the balance.
- **Price changes.** We may change the price of a plan. We will email you at least 30 days before
  a price change affects you. If you do not want the new price, cancel before it takes effect and
  you will never be charged it.

### 4. Your data

- **You own it.** Everything you send to FaultLine remains yours. We claim no ownership of it.
- **We do not train on it.** We do not use your data to train, fine-tune, or evaluate any machine
  learning model, ours or anyone else's. We do not sell it. We do not share it with third parties
  except the infrastructure providers strictly required to run the service.
- **Isolation.** Your data is held in dedicated PostgreSQL schemas that belong to your account
  alone, and every query is bound to them. No other customer's queries can reach your schemas.
  Some platform-level records — your account, your subscription, your signed agreements — are held
  in a shared control-plane database, separated by account and access-controlled; those contain
  billing and identity information, never the contents of your memory.
- **Export.** You can request a complete export of your data, in a machine-readable format, at any
  time by emailing support@volenti.ca from an address on your account. We will deliver it within
  {{FULFILMENT_DAYS}} business days. You can request an export while your subscription is active and at any point
  during the retention window in section 7. We do not charge for exports.
- **Deletion on request.** You can request permanent deletion of your data at any time by emailing
  support@volenti.ca. We will complete it within {{FULFILMENT_DAYS}} business days and confirm in writing.
  Deletion is permanent and we cannot reverse it. Any backup copies containing the deleted data are
  overwritten within a further {{BACKUP_OVERWRITE_DAYS}} days.
- **Privacy law.** We handle personal information in accordance with the Personal Information
  Protection and Electronic Documents Act (PIPEDA). The commitments in this section are the
  binding ones; if we publish a separate privacy policy, it explains our practices in more detail
  but does not reduce anything promised here. Privacy questions: privacy@volenti.ca.
- **Where your data is held.** We will tell you, on request to privacy@volenti.ca, the country in
  which your data is stored and processed. If we move it to a different country we will email you
  at least 30 days beforehand, and you may cancel before the change takes effect under section 3.

### 5. Acceptable use

You agree not to use FaultLine to:

- store or process data you do not have the legal right to store or process;
- break any law that applies to you or to us;
- attempt to access another customer's data, or to probe, scan, or disrupt the service or its
  infrastructure;
- resell or provide the service to third parties as your own product, unless we have agreed to
  that in writing.

If you breach this section we may suspend your account immediately to stop ongoing harm. We will
tell you why, in writing, within one business day, and we will give you 10 days to fix the problem
before we terminate. For a breach that exposes us or another customer to immediate legal or
security risk, we may terminate without that 10-day period.

### 6. Availability, warranties, and liability

The service is provided "as is". To the extent the law allows, we exclude all implied warranties,
including merchantability and fitness for a particular purpose.

**Our total liability to you, for all claims arising out of or relating to this agreement, is
limited to the fees you actually paid us in the 12 months before the event giving rise to the
claim.** We are not liable for indirect, incidental, special, or consequential damages, or for
lost profits, lost revenue, or lost or corrupted data, even if we were told such damages were
possible.

Nothing in this agreement limits liability that cannot be limited by law, including liability for
fraud, for fraudulent misrepresentation, or for death or personal injury caused by negligence.

Because we exclude liability for lost data, and because you can export at any time under section
4: **keep your own backups of anything you cannot afford to lose.**

### 7. Term, termination, and what happens to your data

This agreement starts when you accept it and runs for the term set out in section 3, renewing
for further terms of the same length unless you tell us not to.

You may stop it renewing at any time (section 3); the term you are in runs to its end. We may
terminate for your breach of section 5, or for any reason on 30 days' written notice to you — and
if we terminate for any reason other than your breach, we refund every month of the term you have
paid for and not used, calculated by the day.

When your subscription ends, for any reason:

1. Your data is retained, and available for export on request under section 4, for **{{RETENTION_DAYS}} days**.
2. At the end of those {{RETENTION_DAYS}} days it is permanently deleted from our live systems.
3. Any backup copies containing your data are overwritten within a further **{{BACKUP_OVERWRITE_DAYS}} days**.

If you want your data deleted sooner, request deletion under section 4 before the subscription
ends.

### 8. Changes to this agreement

We may publish a new version of this agreement. If a change materially reduces your rights or
increases your obligations, we will email you at least 30 days before it applies to you, and you
may cancel before it takes effect rather than accept it. Non-material changes (clarifications,
typo fixes, updated contact details) take effect when published.

The version you accepted is recorded in full with your signature. You are shown its contract
number the moment you sign, and you can obtain a complete copy at any time by emailing
legal@volenti.ca with that number; we will send it within {{FULFILMENT_DAYS}} business days. A later version
never changes the terms you agreed to for a period you have already paid for.

### 9. Governing law

This agreement is governed by the laws of the Province of Ontario and the laws of Canada that
apply in it. The courts of Ontario have exclusive jurisdiction over any dispute arising out of it,
and both of us submit to those courts.

### 10. General

- **Whole agreement.** This document is the entire agreement between us about the service. It
  replaces any earlier discussion or proposal.
- **Assignment.** You may not transfer this agreement without our written consent. We may transfer
  it to a successor if our business is sold, on notice to you.
- **Severability.** If a court finds any part of this agreement unenforceable, the rest continues
  to apply.
- **Notices.** We send notices to the email address on your account. You send notices to
  legal@volenti.ca. Keep your email address current — it is how we reach you.

---

FaultLine is operated by Volenti. Questions about these terms: legal@volenti.ca. Security
issues: security@volenti.ca. Privacy: privacy@volenti.ca. Billing and cancellation:
billing@volenti.ca.$terms$,
    NOW(),
    'system-seed'
)
ON CONFLICT (version) DO NOTHING;
