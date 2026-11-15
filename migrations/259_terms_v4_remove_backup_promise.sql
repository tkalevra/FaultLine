-- 259_terms_v4_remove_backup_promise.sql
--
-- Publish Subscription Agreement **2026-08-13-v4**: REMOVE the backup-overwrite promise, because
-- there is no backup tooling in this repository and the promise was therefore false.
--
-- THE FINDING
-- -----------
-- A paralegal review traced all 64 factual claims in the agreement to a code consumer. Sections 4
-- and 7 each promised that "any backup copies containing your data are overwritten within a
-- further 35 days". A repo-wide search finds NO backup mechanism of any kind — not a job, not a
-- script, not a scheduled task, not a documented manual procedure. The clause described a control
-- that does not exist.
--
-- WHY THAT IS URGENT RATHER THAN UNTIDY: `signed_contracts` snapshots the body at acceptance and
-- is append-only, so a false clause becomes PERMANENT for whoever signs it. For a consumer,
-- Ontario's Consumer Protection Act, 2002 s.18(1) makes a false, misleading or deceptive
-- representation a ground for rescission. The count of signatures against v3 is currently ZERO;
-- this costs one migration today and is unfixable per-customer tomorrow.
--
-- EXACTLY TWO SENTENCES REMOVED, AND ONE DELIBERATELY KEPT
-- -------------------------------------------------------
--   * S4 "Deletion on request" — dropped the trailing backup sentence. The deletion promise
--     itself ("permanent and we cannot reverse it") is unchanged and IS implemented.
--   * S7 — removed item 3 of the end-of-subscription list. Items 1 and 2 (the retention window
--     and the permanent deletion at the end of it) are unchanged.
--   * S6 KEEPS "keep your own backups of anything you cannot afford to lose." That is ADVICE TO
--     THE CUSTOMER, not a promise by us, and with no backups on our side it is more important
--     than it was, not less. Removing it would be the wrong kind of tidy.
--
-- WHAT THIS DOES NOT DO: it does not create backups. The operational fact — that a memory product
-- has no backup story — is unchanged and is a business risk on its own terms, independent of what
-- any document says. This migration only stops the agreement from claiming otherwise.
--
-- MATERIALITY: TRUE. Removing a protection the customer was promised reduces their rights, which
-- is section 8's own test. Nobody has signed, so no notice is owed in practice, but the flag
-- records what it would have required.
--
-- The full text is restated below so the agreement reads as one document. It was transcribed
-- mechanically from the v3 body with two anchored deletions, so it cannot have drifted.
--
-- IDEMPOTENT: returns early if 2026-08-13-v4 is already present.

BEGIN;

DO $migration$
DECLARE
    v_active_id BIGINT;
    v_has_rv    BOOLEAN;
    v_has_cl    BOOLEAN;
BEGIN
    IF EXISTS (SELECT 1 FROM public.contract_templates WHERE version = '2026-08-13-v4') THEN
        RAISE NOTICE '259: version 2026-08-13-v4 already published — no-op.';
        RETURN;
    END IF;

    SELECT id INTO v_active_id FROM public.contract_templates
     WHERE retired_at IS NULL ORDER BY effective_at DESC, id DESC LIMIT 1;

    UPDATE public.contract_templates SET retired_at = NOW() WHERE retired_at IS NULL;

    INSERT INTO public.contract_templates (version, title, body_markdown, effective_at, created_by)
    VALUES (
        '2026-08-13-v4',
        'FaultLine Subscription Agreement',
        $terms$## FaultLine Subscription Agreement

**Effective date of this version:** August 13, 2026

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

You choose a plan, and you add seats to it. Each seat is one named person or one automated agent.

**Seats are not part of your term commitment.** You may ADD a seat from your console at any time,
and you may REMOVE one at any time. Both take effect on your NEXT monthly invoice: an added seat
first appears on the next invoice, and a removed seat drops off it. We do not charge you a part
month for a seat you added mid-month, and we do not refund the part month for a seat you removed
mid-month — the seat stays usable until that invoice date, because you have paid for it. Your team
can grow and shrink freely without renegotiating or extending anything.

What IS committed is your plan's base fee, for the term set out in section 3. Removing seats does
not reduce that base fee, and removing every seat does not end your term.

You are responsible for who you give access to, and for what they do with it. Keep credentials
secure and tell us promptly at security@volenti.ca if you believe an account has been compromised.

### 3. Fees, billing, and cancellation

- **What you pay.** The plan and monthly fee shown on the checkout page at the moment you accept
  this agreement. That amount, the currency, and the billing interval are recorded with your
  signature and are shown on your receipt.
- **When you pay.** Monthly, in advance, on the day of the month you first subscribed, charged to
  the payment method you provide. Payments are processed by Stripe; we never see or store your
  full card number.
- **One invoice.** Your base fee, your seats, and any add-ons are billed together on a single
  monthly invoice from Stripe. That invoice is the record of what you were charged.
- **Taxes.** Fees are exclusive of applicable taxes. Canadian sales tax is calculated and added at
  checkout based on your billing address.
- **Your commitment.** This is a **{{CONTRACT_TERM_MONTHS}}-month term**, starting on the day you subscribe. What you
  are committing to for that period is your plan's **base fee** — not a seat count, and not a
  minimum spend. We bill it in monthly instalments rather than asking for the whole term up
  front — the monthly charge is how you PAY, not how long you are committed. Your seats and any
  add-ons sit on top of that base fee and stay adjustable throughout (section 2). Please be sure
  the base commitment is what you want before you sign: it is the one part of this agreement you
  cannot get out of early.
- **Renewal.** At the end of the term your subscription renews for a further {{CONTRACT_TERM_MONTHS}}-month term unless
  you tell us otherwise. To stop it renewing, email billing@volenti.ca at any time before the term
  ends; you keep the service to the end of the term you have paid for and are not charged again.
- **Cancelling during a term.** You may stop using the service at any time and we will confirm
  within one business day. Cancelling stops the service; it does not end your commitment. The
  base fee for the rest of your term is still owed, and the next bullet sets out exactly how it
  is collected and the two ways you can resolve it — **read the two together, they describe one
  process, not two.** We do not refund the balance of a term. Seats and add-ons stop being
  charged from the next invoice, as set out in section 2; only the base fee is affected.
- **If your subscription ends before your term does.** If you cancel during a term, or if we
  close your account after a suspension for non-payment, the base fee for the remaining months
  of your term becomes due as a single amount and we will send you one final invoice for it,
  stating the months and the amount. **Only the base fee is included — seats and add-ons are
  not**, because they were never part of your commitment (section 2). You then have two ways to
  resolve it, and the choice is yours:
    1. **Settle it.** Pay that final invoice and your account is closed with nothing further
       owed.
    2. **Resume.** Bring your subscription back and we cancel that final invoice entirely; you
       simply continue paying monthly for the rest of your term as before.
  We will set out both options, and the exact amount, in writing at the time. We will not send
  the amount to collections, or charge interest on it, without giving you at least 30 days from
  that written notice to choose one of the two.
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
  settling the balance. Suspension for non-payment does not end your term or cancel what is owed
  for it.
- **Price changes.** We may change the price of a plan. We will email you at least 30 days before
  a price change affects you. If you do not want the new price, cancel before it takes effect and
  you will never be charged it. A price change never applies to a term you are already in — the
  base fee recorded with your signature is the base fee for that whole term.

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
  Deletion is permanent and we cannot reverse it.
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

If you want your data deleted sooner, request deletion under section 4 before the subscription
ends.

### 8. Changes to this agreement

We may publish a new version of this agreement. If a change materially reduces your rights or
increases your obligations, we will email you at least 30 days before it applies to you, and you
may cancel before it takes effect rather than accept it. Non-material changes (clarifications,
typo fixes, updated contact details) take effect when published.

The version you accepted is recorded in full with your signature, together with its version
number. You are shown its contract number the moment you sign, and you can obtain a complete copy
at any time by emailing legal@volenti.ca with that number; we will send it within {{FULFILMENT_DAYS}} business
days. **A later version never changes the terms you agreed to.** If we publish a new version, the
one you signed continues to govern your subscription for the term you signed it for; you move to a
newer version only on renewal, and only after the notice described above.

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
        'migration-259'
    );

    SELECT EXISTS (SELECT 1 FROM pg_attribute
         WHERE attrelid = to_regclass('public.contract_templates')
           AND attname = 'retention_values' AND NOT attisdropped AND attnum > 0) INTO v_has_rv;
    IF v_has_rv AND v_active_id IS NOT NULL THEN
        EXECUTE 'UPDATE public.contract_templates t SET retention_values = p.retention_values '
                '  FROM public.contract_templates p WHERE t.version = $1 AND p.id = $2'
        USING '2026-08-13-v4', v_active_id;
    END IF;

    SELECT EXISTS (SELECT 1 FROM pg_attribute
         WHERE attrelid = to_regclass('public.contract_templates')
           AND attname = 'change_summary' AND NOT attisdropped AND attnum > 0) INTO v_has_cl;
    IF v_has_cl THEN
        EXECUTE 'UPDATE public.contract_templates SET change_summary = $1, '
                '       change_is_material = TRUE, supersedes_version = $2 WHERE version = $3'
        USING
          'We removed the promise that backup copies of your data are overwritten within 35 days. '
          'We removed it because it was not true: we do not currently keep backups, so there was '
          'no such process to describe, and we would rather say nothing than say something '
          'inaccurate about your data. Nothing else changed. Your data is still retained for 30 '
          'days after your subscription ends and then permanently deleted, you can still request '
          'export or permanent deletion at any time, and deletion is still permanent and '
          'irreversible. Because we do not keep backups, the advice in section 6 matters more '
          'than it did: keep your own copy of anything you cannot afford to lose.',
          '2026-08-13-v3', '2026-08-13-v4';
    END IF;

    RAISE NOTICE '259: published 2026-08-13-v4 (superseding template id %) — the backup promise is '
                 'gone. It described a control that does not exist.', v_active_id;
END
$migration$;

COMMIT;
