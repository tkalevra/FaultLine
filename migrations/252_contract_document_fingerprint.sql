-- 252_contract_document_fingerprint.sql — bind the terms the CUSTOMER SAW to the signature we record
--
-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- THE DEFECT THIS CLOSES
-- ═══════════════════════════════════════════════════════════════════════════════════════════
--
-- Migration 243 gave every acceptance an immutable ``contract_snapshot`` + ``contract_version``.
-- That records WHAT WE WROTE. It does not — and cannot — prove that what we wrote is what the
-- customer's browser RENDERED. ``GET /tenant/plans`` serves the active terms; the POST re-reads
-- the active terms. Two reads of a mutable table with a human reading time in between.
--
-- Migration 243 + tenant_checkout.begin_subscription already close HALF of it: the client echoes
-- ``contract_template_id`` and the server refuses (``terms_changed``) when the active TEMPLATE ROW
-- moved. That guards the row IDENTITY. It does not guard the DOCUMENT, and in THIS codebase those
-- are genuinely different things:
--
--     saas/contracts.py::_substitute_for_display interpolates LIVE platform settings
--     ({{RETENTION_DAYS}}, {{BACKUP_OVERWRITE_DAYS}}, {{FULFILMENT_DAYS}},
--     {{CONTRACT_TERM_MONTHS}}) into the seeded template AT READ TIME.
--
-- So an operator who changes public.platform_settings.data_retention_days between the page load
-- and the click changes the WORDS the customer signs while ``contract_templates.id`` and
-- ``contract_templates.version`` both stay exactly the same. The id binding cannot see it. The
-- customer reads "retained for 30 days"; the permanent, undeletable row says "7 days".
--
-- A VERSION STRING IS A LABEL. A HASH IS THE DOCUMENT. This migration adds the document
-- fingerprint on both sides of the binding.
--
-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- WHY A FINGERPRINT, AND NOT JUST A TIGHTER VERSION DISCIPLINE — the legal ground
-- ═══════════════════════════════════════════════════════════════════════════════════════════
--
--   * Ironclad, "6 Components of Clickwrap Enforceability"
--     https://ironcladapp.com/journal/contract-management/6-components-of-clickwrap-enforceability
--     "Unless you can prove who accepted your agreements and which version(s) they accepted, your
--      clickwrap is not likely to be enforceable" — and, on why the burden cannot be shifted to
--      the customer, "courts have found that customers have no obligation to check the terms
--      periodically for changes."  On the record of assent: "By tracking crucial acceptance data
--      like time of acceptance, version of agreement, and other data that ties a specific user to
--      an acceptance event, you will increase clickwrap enforceability in court."
--
--   * ESIGN, 15 U.S.C. § 7001(d)(1)(A) — an electronic record satisfies a retention requirement
--     only if it "accurately reflects the information set forth in the contract or other record",
--     and (d)(1)(B) remains "in a form that is capable of being accurately reproduced for later
--     reference".  https://www.law.cornell.edu/uscode/text/15/7001
--     ACCURATELY REFLECTS is the operative phrase: a snapshot of a document the signer never saw
--     is a faithful record of the wrong thing. The fingerprint is what makes "this is the document
--     they were shown" a checkable claim rather than an assertion.
--
-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- WHAT THIS MIGRATION ADDS
-- ═══════════════════════════════════════════════════════════════════════════════════════════
--
--   1. public.contract_templates.body_sha256  — the fingerprint of the STORED body_markdown.
--      Backfilled here for every existing row. Served to the browser by GET /tenant/plans and
--      echoed back on the subscribe POST.
--   2. public.signed_contracts.contract_sha256 — the fingerprint of the text actually snapshotted
--      at acceptance. Written by saas/contracts.py::record_acceptance_guarded on every new
--      signature.
--   3. Both columns join their table's append-only trigger (see the two decisions below).
--
-- THE HASH IS COMPUTED IDENTICALLY IN SQL AND IN PYTHON, and that equality is PINNED BY A TEST
-- (tests/test_terms_version_binding.py::test_postgres_and_python_agree_on_the_fingerprint) rather
-- than assumed:
--     SQL    : encode(sha256(convert_to(body_markdown, 'UTF8')), 'hex')
--     Python : hashlib.sha256(body.encode("utf-8")).hexdigest()
-- ``sha256()`` returns bytea (PostgreSQL 11+); ``convert_to(text,'UTF8')`` pins the server encoding
-- out of the answer, so the fingerprint does not depend on the database's client_encoding.
-- Lowercase hex both sides (``encode(...,'hex')`` and ``.hexdigest()`` are both lowercase).
--
-- ⚠️ ORDER INSIDE THIS FILE IS LOAD-BEARING: the BACKFILL must run BEFORE body_sha256 joins the
-- immutable trigger, or the trigger refuses its own migration's UPDATE. Do not reorder.

-- ── 1. contract_templates.body_sha256 ──────────────────────────────────────────────────────
ALTER TABLE public.contract_templates ADD COLUMN IF NOT EXISTS body_sha256 TEXT;

COMMENT ON COLUMN public.contract_templates.body_sha256 IS
    'SHA-256 of body_markdown, lowercase hex. The DOCUMENT FINGERPRINT the subscribe page echoes '
    'back so the server can refuse a signature against text that is no longer in force. Computed '
    'as encode(sha256(convert_to(body_markdown,''UTF8'')),''hex''); saas/contracts.py::'
    'body_fingerprint() is the one Python implementation and the two are pinned equal by test.';

-- BACKFILL — every existing row. Runs BEFORE the column joins the immutable trigger (below).
-- Idempotent: WHERE body_sha256 IS NULL, so a re-run of this migration matches nothing.
UPDATE public.contract_templates
   SET body_sha256 = encode(sha256(convert_to(body_markdown, 'UTF8')), 'hex')
 WHERE body_sha256 IS NULL;

-- ── 1b. EVERY writer of contract_templates gets the fingerprint — enforced at the TABLE ────
--
-- ⚠️ THIS EXISTS BECAUSE AN AUDIT OF EVERY WRITER FOUND ONE THAT WOULD MISS IT. The writers of
-- public.contract_templates are:
--     (a) saas/contracts.py::publish_terms_version   — the sanctioned operator path;
--     (b) migration 243's seeded first version       — covered by the backfill above;
--     (c) migration 253's '2026-08-12-v2' seed       — a plain INSERT with an explicit column
--         list that does NOT mention body_sha256, and it runs AFTER this migration, so the
--         backfill above can never reach it;
--     (d) any future migration seed or manual INSERT — unknowable from here.
--
-- Teaching (a) to write the column and calling it done would leave (c) and (d) live. A guard on
-- four writers out of five is not a guard. So the fingerprint is DERIVED AT THE TABLE, where no
-- writer can forget it and no writer needs to know it exists:
--
--   * INSERT with body_sha256 NULL/omitted  → filled from body_markdown. (c) and (d) are covered
--     without touching a sibling agent's migration file.
--   * INSERT with body_sha256 SUPPLIED      → VERIFIED against body_markdown and REFUSED if it
--     disagrees. A fingerprint that can be set to an arbitrary string is decoration; this makes
--     the column incapable of holding a value that is not the hash of the row it sits on.
--   * UPDATE                                → write-once + the same verification (in the
--     immutable-columns function below).
--
-- Deterministic and total: one SHA-256 over the body, no configuration, no fallback.
CREATE OR REPLACE FUNCTION public.contract_templates_fingerprint()
RETURNS trigger AS $$
DECLARE
    computed TEXT;
BEGIN
    computed := encode(sha256(convert_to(COALESCE(NEW.body_markdown, ''), 'UTF8')), 'hex');
    IF NEW.body_sha256 IS NULL THEN
        NEW.body_sha256 := computed;
    ELSIF lower(NEW.body_sha256) <> computed THEN
        RAISE EXCEPTION 'public.contract_templates.body_sha256 (%) is not the SHA-256 of the '
                        'body_markdown it was supplied with (%). The fingerprint is DERIVED — '
                        'omit it and it will be computed, or supply the correct value.',
                        NEW.body_sha256, computed;
    ELSE
        NEW.body_sha256 := computed;   -- normalise to lowercase hex
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_contract_templates_fingerprint ON public.contract_templates;
CREATE TRIGGER trg_contract_templates_fingerprint
    BEFORE INSERT ON public.contract_templates
    FOR EACH ROW EXECUTE FUNCTION public.contract_templates_fingerprint();

-- ── 2. signed_contracts.contract_sha256 ────────────────────────────────────────────────────
--
-- ⚠️ DELIBERATELY NULLABLE, AND DELIBERATELY NOT BACKFILLED. Two separate decisions:
--
--   NOT NULL — impossible. The rows predate the column and a NOT NULL on a table that cannot be
--   correctly backfilled is a migration that fails on any deployment with history. The WRITE PATH
--   is what guarantees the value instead (saas/contracts.py stamps it on every INSERT), and that
--   is pinned by test_every_new_signature_is_stamped_with_its_fingerprint.
--
--   NO BACKFILL — a choice, not an oversight. We COULD compute
--   encode(sha256(convert_to(contract_snapshot,'UTF8')),'hex') for the legacy rows, and it would
--   even be arithmetically correct. It would also be a LIE about provenance: it would assert that
--   a fingerprint was recorded at acceptance when no fingerprint existed at that time, on rows
--   whose entire purpose is to be evidence. NULL here reads honestly as "signed before the
--   document fingerprint existed"; the snapshot is still there and can be hashed by any reader at
--   any time, so nothing is lost. Legal records do not get retroactive provenance.
ALTER TABLE public.signed_contracts ADD COLUMN IF NOT EXISTS contract_sha256 TEXT;

COMMENT ON COLUMN public.signed_contracts.contract_sha256 IS
    'SHA-256 (lowercase hex) of contract_snapshot — the fingerprint of the exact text this signer '
    'accepted, and the value the browser echoed back from GET /tenant/plans. An ACCEPTANCE FACT: '
    'immutable, same as contract_snapshot. NULL only on rows signed before migration 252.';

CREATE INDEX IF NOT EXISTS idx_signed_contracts_sha256
    ON public.signed_contracts (contract_sha256);

-- ── 3a. contract_sha256 joins the signed_contracts acceptance-fact list ────────────────────
--
-- STRICTLY IMMUTABLE, exactly like contract_snapshot. The task-level question was whether an
-- acceptance fact that can be NULL on legacy rows should be write-once (NULL → value permitted)
-- instead. It must NOT be: write-once would leave a door open to STAMP a fingerprint onto a
-- historical row later, which is precisely the retroactive-provenance lie the no-backfill decision
-- above refuses. A legacy NULL stays NULL forever; that is the correct, honest record.
--
-- Follows migration 243's EXISTING column-aware pattern verbatim (DECLARE bad TEXT; + a chained
-- ``IS DISTINCT FROM ... AND bad IS NULL`` per column, one RAISE at the end). This is a
-- CREATE OR REPLACE of that function with one clause added — not a replacement of the pattern.
CREATE OR REPLACE FUNCTION public.signed_contracts_immutable_columns()
RETURNS trigger AS $$
DECLARE
    bad TEXT;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'public.% is append-only — TRUNCATE is not permitted. These rows are '
                        'permanent financial records.', TG_TABLE_NAME;
    END IF;

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
    -- migration 252 — the fingerprint of the snapshot. As immutable as the snapshot it fingerprints:
    -- a mutable hash of an immutable document proves nothing at all.
    IF NEW.contract_sha256        IS DISTINCT FROM OLD.contract_sha256        AND bad IS NULL THEN bad := 'contract_sha256';        END IF;
    IF NEW.contract_version       IS DISTINCT FROM OLD.contract_version       AND bad IS NULL THEN bad := 'contract_version';       END IF;
    IF NEW.accepted_at            IS DISTINCT FROM OLD.accepted_at            AND bad IS NULL THEN bad := 'accepted_at';            END IF;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'public.signed_contracts.% is an acceptance fact — it is append-only and '
                        'cannot be modified. Only status, stripe_checkout_session_id, '
                        'stripe_subscription_id and account_id may advance.', bad;
    END IF;

    -- LIFECYCLE LINKAGE — WRITE-ONCE.
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

    -- STATUS — forward-only.
    IF OLD.status = 'completed' AND NEW.status = 'accepted' THEN
        RAISE EXCEPTION 'public.signed_contracts.status cannot regress from completed to accepted.';
    END IF;
    IF OLD.status = 'voided' AND NEW.status <> 'voided' THEN
        RAISE EXCEPTION 'public.signed_contracts.status cannot advance out of voided.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── 3b. body_sha256 joins the contract_templates trigger — WRITE-ONCE, not strictly immutable ──
--
-- THE DECISION, AND WHY IT DIFFERS FROM contract_sha256 ABOVE (migration 252 asks this to be
-- stated rather than assumed, and the two columns genuinely warrant different answers):
--
--   * ``contract_sha256`` on a SIGNED row is EVIDENCE OF AN EVENT. It can only ever be correct if
--     it was written at the moment of the event, so a later write is never a repair — it is a
--     fabrication. STRICTLY IMMUTABLE (above).
--
--   * ``body_sha256`` on a TEMPLATE is a DERIVED PROPERTY of a column that is itself already
--     immutable (243 refuses any UPDATE to body_markdown). Because the source cannot change, the
--     correct hash is a fixed, independently recomputable value — writing it later cannot assert
--     anything false, and it can be verified by anyone with the row. The real risk is the
--     opposite: a NULL fingerprint on a row that entered by some path other than
--     publish_terms_version (a future migration seed, a manual INSERT) would be unfixable under
--     strict immutability, and this migration's own BACKFILL would be refused by the trigger it
--     is adding. So: WRITE-ONCE — NULL → value permitted (the repair), value → different value
--     REFUSED (the tamper).
--
--   That is not a weaker guarantee than immutability here, it is the SAME guarantee reached
--   honestly: since body_markdown cannot change, the only value a NULL→value write can legally
--   take is the one anybody can recompute, and any OTHER value is refused outright.
--
--   ``retired_at`` in migration 243 already uses precisely this write-once shape, so this is the
--   table's existing idiom rather than a new one.
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
    -- migration 252 — WRITE-ONCE (see the decision block above this function), and the ONE value
    -- a NULL -> value repair may take is the correct hash. Write-once alone would still let a
    -- repair write a WRONG fingerprint once, permanently; verifying it means the column can only
    -- ever hold the hash of the body it sits on.
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
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The triggers themselves are unchanged (243 created them BEFORE UPDATE OR DELETE FOR EACH ROW,
-- plus the two BEFORE TRUNCATE statement triggers). CREATE OR REPLACE FUNCTION above is picked up
-- by the existing triggers with no re-CREATE needed — but re-assert them anyway so this migration
-- is safe to apply to a schema built from an older cut of 243 that lacked one of them.
DROP TRIGGER IF EXISTS trg_signed_contracts_immutable ON public.signed_contracts;
CREATE TRIGGER trg_signed_contracts_immutable
    BEFORE UPDATE OR DELETE ON public.signed_contracts
    FOR EACH ROW EXECUTE FUNCTION public.signed_contracts_immutable_columns();

DROP TRIGGER IF EXISTS trg_contract_templates_immutable ON public.contract_templates;
CREATE TRIGGER trg_contract_templates_immutable
    BEFORE UPDATE OR DELETE ON public.contract_templates
    FOR EACH ROW EXECUTE FUNCTION public.contract_templates_immutable_columns();
