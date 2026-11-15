-- 256_contract_changelog_and_frozen_terms.sql
--
-- TWO REPAIRS TO THE SAME PROBLEM: A PUBLISHED AGREEMENT MUST BE A FIXED DOCUMENT, AND A CUSTOMER
-- MUST BE ABLE TO SEE WHAT CHANGED.
--
--   A. FREEZE THE NUMBERS a published version states, so its words stop moving.
--   B. RECORD WHAT CHANGED between versions, so a new version can be NOTICED rather than merely
--      published.
--
-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- PART A — WHY THE WORDS OF A PUBLISHED VERSION CURRENTLY MOVE
-- ═══════════════════════════════════════════════════════════════════════════════════════════
--
-- saas/contracts.py's design rule is "BIND CONFIG TO CONTRACT AT PUBLISH, NEVER AT RENDER":
-- ``publish_terms_version`` substitutes {{RETENTION_DAYS}} / {{BACKUP_OVERWRITE_DAYS}} /
-- {{FULFILMENT_DAYS}} / {{CONTRACT_TERM_MONTHS}} ONCE and stores literal text, so from that
-- instant the version is frozen prose. That rule is correct and it works — for versions published
-- through that function.
--
-- IT DOES NOT COVER THE ONES THAT MATTER, and that is the defect. The agreement actually in force
-- is seeded by SQL — migration 243's '2026-08-12-v1' and migration 253's '2026-08-12-v2'. Neither
-- can call a Python function, so both store their body WITH THE RAW ``{{TOKENS}}`` still in it
-- (measured: 4 distinct tokens in 243, the same 4 in 253). The only thing that resolves them is
-- ``_substitute_for_display`` — a READ-SIDE net, running against the LIVE platform settings on
-- every single read.
--
-- Consequence, measured: an operator moving ``data_retention_days`` from 30 to 7 changes the words
-- of the agreement in force, with ``contract_templates.id`` AND ``.version`` both byte-identical.
-- A customer reading "retained for 30 days" who clicks accept a moment later gets a permanent,
-- undeletable ``signed_contracts`` row saying 7. Migration 252's document fingerprint DETECTS this
-- (the signature is refused with a 409 rather than recorded wrongly) — but detecting it means a
-- customer mid-signature is bounced every time an operator touches a setting. The fingerprint is
-- the alarm; this is the fix.
--
-- WHAT THIS DOES: stamps ``retention_values`` — the column migration 246 already added for exactly
-- these numbers — onto every template that still carries tokens, from the values in force NOW.
-- saas/contracts.py then substitutes from the ROW'S OWN FROZEN SET in preference to live settings.
-- After this, changing a platform setting changes what the NEXT published version will say, and
-- nothing else. Which is precisely what migration 246's own comment says the column is for:
-- "version 2026-08-12-v1 was published when data_retention_days was 30".
--
-- ⚠️ THIS DOES NOT AND MUST NOT TOUCH ``body_markdown``. That column is immutable under migration
-- 243's trigger, and rightly so — customers have signed this text. Freezing the VALUES the tokens
-- resolve to achieves the same outcome without editing a published document. Signed rows are
-- unaffected either way: ``signed_contracts.contract_snapshot`` is their own frozen copy.
--
-- ⚠️ WHY "the values in force NOW" IS THE HONEST CHOICE. We cannot know what the settings were on
-- the day each version was published — nothing recorded them, which is the whole defect. What we
-- CAN say truthfully is: from this migration forward, this version says this. Every signature
-- already taken keeps its own snapshot of whatever it was shown, so no existing evidence changes.
--
-- ⚠️ THE TOKEN → COLUMN MAP IS STATED TWICE — here in SQL and in
-- ``saas/contracts.py::_RETENTION_PLACEHOLDER_SOURCES``. That duplication is deliberate (a
-- migration cannot import Python) and is PINNED BY A TEST
-- (test_contract_changelog.py::test_the_migration_and_the_module_agree_on_the_token_map), so drift
-- becomes a red test rather than a silently wrong contract number.

DO $$
DECLARE
    frozen INTEGER := 0;
BEGIN
    -- Guarded on both columns: 246 (retention_values) and the platform_settings columns it reads.
    IF to_regclass('public.contract_templates') IS NULL
       OR to_regclass('public.platform_settings') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_attribute
                       WHERE attrelid = to_regclass('public.contract_templates')
                         AND attname = 'retention_values' AND NOT attisdropped) THEN
        RAISE NOTICE '256A: contract_templates.retention_values or platform_settings absent — '
                     'skipping the freeze (the read-side net keeps working).';
    ELSE
        UPDATE public.contract_templates t
           SET retention_values = jsonb_build_object(
                   'RETENTION_DAYS',        ps.data_retention_days,
                   'BACKUP_OVERWRITE_DAYS', ps.backup_overwrite_days,
                   'FULFILMENT_DAYS',       ps.fulfilment_days,
                   'CONTRACT_TERM_MONTHS',  ps.contract_term_months,
                   'frozen_by',             'migration-256')
          FROM public.platform_settings ps
         WHERE ps.singleton = TRUE
           AND t.retention_values IS NULL
           AND t.body_markdown LIKE '%{{%';
        GET DIAGNOSTICS frozen = ROW_COUNT;
        RAISE NOTICE '256A: froze the contract numbers on % template(s) that still carried '
                     'placeholders. Their words no longer move when an operator edits a '
                     'retention setting.', frozen;
    END IF;
END $$;

-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- PART B — THE CHANGELOG: "what changed", and whether it was MATERIAL
-- ═══════════════════════════════════════════════════════════════════════════════════════════
--
-- WHY THIS IS NOT COSMETIC. Section 8 of the agreement itself distinguishes two kinds of change:
--
--     "If a change materially reduces your rights or increases your obligations, we will email
--      you at least 30 days before it applies to you, and you may cancel before it takes effect
--      rather than accept it. Non-material changes (clarifications, typo fixes, updated contact
--      details) take effect when published."
--
-- So the material/non-material distinction is already a BINDING COMMITMENT in a signed document,
-- and until now nothing in the schema recorded which kind a given version was — the operator made
-- the distinction in their head, if at all, and it left no trace. A promise whose trigger condition
-- is not recorded anywhere cannot be audited and cannot be honoured reliably.
--
-- And on the notice itself, courts have been explicit that publishing new text is not enough:
--   * Ironclad, "6 Components of Clickwrap Enforceability" —
--     https://ironcladapp.com/journal/contract-management/6-components-of-clickwrap-enforceability
--     "courts have ruled that providing revised terms without indicating what changed was not
--      sufficient notice", and for an existing customer base you need "a process to notify
--      existing users of the changes and, ideally, capture their consent to the new version."
--     Same source on why the burden cannot be pushed onto the customer: "courts have found that
--     customers have no obligation to check the terms periodically for changes."
--   * ESIGN 15 U.S.C. § 7001(d)(1)(A) — https://www.law.cornell.edu/uscode/text/15/7001 — a
--     retained record must "accurately reflect the information set forth in the contract".
--     A version history with no statement of what changed between entries does not.
--
-- ``change_summary`` is the "indicating what changed" half, in plain language, stored WITH the
-- version so it cannot drift from it. ``change_is_material`` is the section-8 flag that decides
-- which notice regime applies. ``supersedes_version`` names what it is a change RELATIVE TO —
-- "what changed" is meaningless without it.
--
-- ⚠️ WHAT THIS DOES **NOT** DO: it does not email anybody, and it does not capture re-consent.
-- Those are a product decision about who is notified when, and the owner has not made it. This
-- migration builds the DATA a notice needs. See the report / the module docstring for exactly what
-- remains before section 8 is honoured.

ALTER TABLE public.contract_templates ADD COLUMN IF NOT EXISTS change_summary TEXT;
ALTER TABLE public.contract_templates ADD COLUMN IF NOT EXISTS change_is_material BOOLEAN;
ALTER TABLE public.contract_templates ADD COLUMN IF NOT EXISTS supersedes_version TEXT;

COMMENT ON COLUMN public.contract_templates.change_summary IS
    'PLAIN-LANGUAGE statement of what changed in this version relative to supersedes_version. '
    'Required by saas/contracts.py::publish_terms_version whenever a version supersedes an '
    'incumbent — "providing revised terms without indicating what changed" has been held to be '
    'insufficient notice. NULL only on the first version (nothing to compare to) and on rows '
    'seeded before migration 256.';
COMMENT ON COLUMN public.contract_templates.change_is_material IS
    'TRUE when the change materially reduces the customer''s rights or increases their '
    'obligations (agreement section 8). Material changes carry a 30-day advance notice and a '
    'right to cancel before they apply; non-material changes take effect on publication. NULL = '
    'not stated (first version, or pre-256 row).';
COMMENT ON COLUMN public.contract_templates.supersedes_version IS
    'The version string this one replaced, or NULL for the first version. Makes change_summary '
    'interpretable — "what changed" needs a referent.';

-- ── BACKFILL, HONESTLY ─────────────────────────────────────────────────────────────────────
-- Runs BEFORE these columns join the immutable trigger below (same ordering rule as migration
-- 252: a column cannot be backfilled after it is made immutable). Both statements are keyed on
-- the column still being NULL, so a re-run of this migration matches nothing.
--
-- v1 — the FIRST version. It supersedes nothing, so it has no changelog and saying otherwise
-- would be an invention. ``change_is_material`` is left NULL for the same reason: a first
-- agreement is not a "change" at all, and FALSE would assert it was a non-material one.
UPDATE public.contract_templates
   SET change_summary = 'First published version of the FaultLine Subscription Agreement. '
                        'There is no earlier version, so there is nothing to compare it to.'
 WHERE version = '2026-08-12-v1' AND change_summary IS NULL;

-- ── v2 ─────────────────────────────────────────────────────────────────────────────────────
--
-- 🔴 THIS SUMMARY WAS REWRITTEN BECAUSE THE FIRST ONE WAS FALSE, AND FALSE IN THE DIRECTION
--    THAT MATTERS. It ended: "Nothing else in the agreement changed: the base fee, the term
--    length, your data rights, and the liability terms are unchanged." It was written from
--    migration 253's HEADER — a description of intent — rather than from the two bodies.
--
--    A real diff of the seeded v1 and v2 bodies (4 hunks, 103 diff lines) shows v2 ALSO adds a
--    whole new clause accelerating the remaining base fee into a single immediately-due invoice,
--    plus four other substantive changes the summary never mentioned. An affirmative "nothing
--    else changed" printed beside a NEW ACCELERATION OF LIABILITY is not an incomplete notice,
--    it is a MISLEADING one — and the changelog IS the notice record. This feature exists because
--    courts have held that "providing revised terms without indicating what changed was not
--    sufficient notice"; a summary that actively denies the change is worse than the silence the
--    column was added to fix.
--
--    METHOD, so this is repeatable: the summary is DERIVED FROM THE DIFF OF THE TWO STORED
--    BODIES, never from the intent stated in a migration header. What a version says is what its
--    body says.
--
-- WHAT ACTUALLY CHANGED (every item verified against the diff, section by section):
--   §2  seats leave the term commitment; add/remove any time, effective next invoice, no
--       part-month charge and no part-month refund.  [was: every provisioned seat billed to
--       term-end, no refund]
--   §3  "Your commitment" now names the plan's BASE FEE as the committed thing — not a seat
--       count and not a minimum spend.
--   §3  NEW "One invoice": base fee, seats and add-ons billed together on one Stripe invoice.
--   §3  "Cancelling during a term" narrowed to the BASE FEE instalments; seats and add-ons stop
--       being charged from the next invoice.
--   §3  NEW "If your subscription ends before your term does" — THE ACCELERATION CLAUSE.
--   §3  "Failed payments" adds that suspension does not end the term or cancel what is owed.
--   §3  "Price changes" adds that a price change never applies to a term already running.
--   §8  the version number is recorded with the signature, and grandfathering is stated
--       outright (the signed version governs the whole term; you move on renewal only).
--   UNCHANGED, confirmed by the diff: the base fee AMOUNT, the term LENGTH, sections 4–7 (your
--   data, security, privacy, retention/deletion), the liability terms and the governing law.
--
-- ── change_is_material: FLIPPED TO **TRUE** ────────────────────────────────────────────────
--
-- It was FALSE, justified as "moves in the customer's favour". That is true IN DOLLARS — seat
-- liability is removed outright and nothing gets more expensive. It is NOT the section 8 test,
-- which is "materially reduces your rights OR **increases your obligations**". ACCELERATION does
-- not increase what is owed; it makes it IMMEDIATELY DUE as one invoice instead of falling due
-- month by month. An obligation that becomes payable at once is a larger obligation IN KIND, and
-- it is precisely the sort of term a customer would want the section 8 notice period to consider.
--
-- THE TIE-BREAK IS THE PRINCIPLE ALREADY WRITTEN INTO saas/contracts.py: **wrongly-material costs
-- an email; wrongly-non-material breaks a written promise.** Section 8 commits us to 30 days'
-- notice and a right to cancel for a material change. Getting that wrong in the FALSE direction
-- means we published a change we had promised to pre-announce, and there is no way to un-publish
-- it. TRUE is the fail-safe direction, so TRUE it is.
--
-- ⚠️ NOT SETTLED PRODUCT POLICY — FLAGGED FOR THE OWNER. This is a legal/commercial judgement,
-- not an engineering one, and the owner may deliberately downgrade it to FALSE on the view that
-- a change removing liability cannot be material however it is timed. That is a defensible read.
-- It must be a DECISION though, recorded here, not a default nobody examined. Note that
-- change_is_material is WRITE-ONCE under the trigger below, so a downgrade after this migration
-- has run needs a deliberate hand (or a new version), which is the correct amount of friction.
UPDATE public.contract_templates
   SET change_summary =
           'Seats are no longer part of your term commitment. You may add or remove a seat at '
           'any time; the change takes effect on your next monthly invoice, with no part-month '
           'charge and no part-month refund (section 2). Previously every seat you provisioned '
           'was billed to the end of the term with no refund. '
           'What you commit to for the term is now stated as your plan''s base fee — not a seat '
           'count and not a minimum spend — and if you cancel during a term, only the base fee '
           'instalments run to the end of it; seats and add-ons stop being charged from the next '
           'invoice (section 3). '
           'We have also added, in section 3: that if your subscription ends before your term '
           'does — because you cancel, or because we close your account after a suspension for '
           'non-payment — the base fee for the remaining months becomes due as a SINGLE FINAL '
           'INVOICE rather than month by month, which you may either settle or cancel entirely '
           'by resuming your subscription, and which we will not send to collections or charge '
           'interest on for at least 30 days after we tell you about it; that your base fee, '
           'seats and add-ons are billed together on one invoice; that a suspension for '
           'non-payment does not end your term or cancel what is owed for it; and that a price '
           'change never applies to a term you are already in. '
           'Section 8 now records the version number alongside your signature and says outright '
           'that the version you signed governs your whole term — you move to a newer version '
           'only on renewal, and only after the notice described there. '
           'Unchanged: the base fee amount, the length of the term, your data rights (sections 4 '
           'to 7), the liability terms and the governing law.',
       -- TRUE — see the reasoning block directly above. The acceleration clause makes an
       -- existing obligation immediately due, which increases it in kind (section 8).
       change_is_material = TRUE,
       supersedes_version = '2026-08-12-v1'
 WHERE version = '2026-08-12-v2' AND change_summary IS NULL;

-- ── the changelog columns join the append-only trigger — WRITE-ONCE ────────────────────────
--
-- WRITE-ONCE, not strictly immutable, and the reasoning is the same shape as migration 252's
-- ``body_sha256`` but reaches the answer from the other direction:
--
--   * STRICTLY IMMUTABLE would be wrong because these columns are NULL on every row that predates
--     them, and a pre-256 version that a human later documents accurately is a REPAIR, not a
--     rewrite. Forbidding it would freeze the gap permanently.
--   * MUTABLE would be wrong because ``change_summary`` is the NOTICE RECORD — the evidence of
--     what a customer was told had changed. Once stated, editing it rewrites what we said, which
--     is the same class of tampering the body itself is protected from.
--
-- So: NULL → value is permitted (documenting an undocumented version), value → different value is
-- REFUSED. Same idiom as ``retired_at`` and ``body_sha256``.
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
    -- migration 252 — the document fingerprint: write-once AND it can only hold the true hash.
    IF OLD.body_sha256 IS NOT NULL AND NEW.body_sha256 IS DISTINCT FROM OLD.body_sha256 THEN
        RAISE EXCEPTION 'public.contract_templates.body_sha256 is write-once (already fingerprinted '
                        'as %) — the body it fingerprints is itself immutable, so a different hash '
                        'can only be wrong.', OLD.body_sha256;
    END IF;
    IF NEW.body_sha256 IS NOT NULL
       AND lower(NEW.body_sha256)
           <> encode(sha256(convert_to(COALESCE(NEW.body_markdown, ''), 'UTF8')), 'hex') THEN
        RAISE EXCEPTION 'public.contract_templates.body_sha256 (%) is not the SHA-256 of this '
                        'row''s body_markdown — the fingerprint is DERIVED and cannot be set to '
                        'anything else.', NEW.body_sha256;
    END IF;
    -- migration 256 — the CHANGELOG / notice record: write-once (see the block above).
    IF OLD.change_summary IS NOT NULL
       AND NEW.change_summary IS DISTINCT FROM OLD.change_summary THEN
        RAISE EXCEPTION 'public.contract_templates.change_summary is write-once — it is the record '
                        'of what customers were told had changed. Editing it rewrites what we '
                        'said; publish a new version instead.';
    END IF;
    IF OLD.change_is_material IS NOT NULL
       AND NEW.change_is_material IS DISTINCT FROM OLD.change_is_material THEN
        RAISE EXCEPTION 'public.contract_templates.change_is_material is write-once — it decides '
                        'which notice regime (agreement section 8) applied to this version.';
    END IF;
    IF OLD.supersedes_version IS NOT NULL
       AND NEW.supersedes_version IS DISTINCT FROM OLD.supersedes_version THEN
        RAISE EXCEPTION 'public.contract_templates.supersedes_version is write-once — it is what '
                        'makes change_summary interpretable.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_contract_templates_immutable ON public.contract_templates;
CREATE TRIGGER trg_contract_templates_immutable
    BEFORE UPDATE OR DELETE ON public.contract_templates
    FOR EACH ROW EXECUTE FUNCTION public.contract_templates_immutable_columns();
