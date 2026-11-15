-- 260_contract_notice_log.sql — the DELIVERY RECORD for contractual notices.
-- CLOSED-SOURCE SaaS layer.
--
-- Migration number: 260. 243–259 are taken; 255 is a DELIBERATE GAP and is not filled here.
-- The runner globs + sorts numerically with no contiguity check, so a gap is harmless.
--
-- WHY THIS TABLE EXISTS — TWO DUTIES AND ONE MISSING COMPONENT
-- ────────────────────────────────────────────────────────────
-- (1) DELIVERY OF THE SIGNED AGREEMENT. Consumer Protection Act, 2002 (Ontario) s.39(1): "A
--     supplier shall deliver to a consumer who enters into an internet agreement a copy of the
--     agreement in writing within the prescribed period after the consumer enters into the
--     agreement." O. Reg. 17/05 s.33(1) prescribes that period: "within 15 days after the
--     consumer enters into the agreement." s.33(3) prescribes the MANNER, and paragraph 4 is the
--     reason this table is a table and not a log line: "Providing it to the consumer in any other
--     manner that allows the supplier to PROVE that the consumer has received it." A send you
--     cannot evidence is, for the purposes of s.39, a send you cannot rely on. CPA 2002 s.40(2):
--     a consumer may cancel the agreement within 30 days if the supplier does not comply with s.39.
--
-- (2) NOTICE OF A NEW VERSION. Section 8 of the Subscription Agreement itself: "If a change
--     materially reduces your rights or increases your obligations, we will email you at least 30
--     days before it applies to you, and you may cancel before it takes effect rather than accept
--     it." saas/contracts.py::publish_terms_version records change_summary + change_is_material
--     precisely so that promise CAN be honoured, and states in its own docstring that it notifies
--     nobody. This table is the other half.
--
-- WHY THE RECORD IS ALSO THE IDEMPOTENCY ARBITER
-- ──────────────────────────────────────────────
-- Sending a customer the same contractual notice twice is a real harm, not a cosmetic one: a
-- duplicated "we are changing your agreement" email reads as a second change. Webhooks replay,
-- sweeps re-run, and a process restart loses in-memory state — so the arbiter is a Postgres ROW,
-- exactly as public.retention_email_log is for the retention lifecycle:
--
--     INSERT ... ON CONFLICT (contract_id, notice_kind, notice_ref) DO UPDATE ... WHERE <retryable>
--
-- and zero affected rows means "already delivered (or in flight) — send nothing". The UNIQUE key
-- IS the contract.
--
-- ⚠️ THE RECORD IS NOT DELETED ON FAILURE, AND THAT IS A DELIBERATE DIFFERENCE FROM
-- public.retention_email_log. That table's sender DELETEs its dedupe row after a failed send so
-- the next sweep retries. Here the row IS the evidence — including the evidence of an attempt that
-- did not land — so a failure is recorded as outcome='failed' and the conditional DO UPDATE
-- re-claims it. Retryability and auditability are both preserved; deleting would buy the first at
-- the cost of the second, on the one table whose entire purpose is proof.
--
-- ⚠️ AND IT NEVER HOLDS THE AGREEMENT TEXT. The signed text lives exactly once, in
-- public.signed_contracts.contract_snapshot (append-only). This table records WHAT WAS SENT by
-- reference — contract id, version, and the document fingerprint — so a second, drifting copy of a
-- legal document cannot come into existence here.
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.
-- Written by saas/contract_notices.py.

CREATE TABLE IF NOT EXISTS public.contract_notice_log (
    id               BIGSERIAL PRIMARY KEY,

    -- The signature this notice is ABOUT. Not a foreign key: public.signed_contracts is
    -- append-only and its rows are never deleted, so referential drift is impossible, and an FK
    -- would make this audit table able to block a write to the financial record it merely
    -- observes. The evidence points at the contract; it does not constrain it.
    contract_id      BIGINT NOT NULL,

    -- 'agreement_copy'        — the statutory delivery of the signed copy (CPA s.39 / O.Reg s.33).
    -- 'version_notice'        — the section 8 notice that a MATERIAL new version was published.
    -- 'agreement_copy_resend' — an operator-requested re-send ("send me my copy again").
    notice_kind      TEXT NOT NULL,

    -- What distinguishes one notice of this kind from another for this contract:
    --   agreement_copy        → '' (there is exactly one statutory delivery per signature).
    --   version_notice        → the NEW template version string (one notice per version).
    --   agreement_copy_resend → a fresh uuid, so a resend is ALWAYS recorded and NEVER suppressed.
    -- Empty string rather than NULL on purpose: NULLs do not conflict in a UNIQUE index, so a
    -- nullable ref would silently disable the exactly-once guarantee for the one kind that needs
    -- it most.
    notice_ref       TEXT NOT NULL DEFAULT '',

    -- The address the copy was sent to, recorded as sent. This is signed_contracts.signer_email —
    -- an immutable acceptance fact — NOT the (self-mutable) login email. See the module docstring
    -- of saas/contract_notices.py for why delivery and authorisation answer that differently.
    to_address       TEXT NOT NULL,

    -- WHAT was delivered, by reference. contract_version is the citation; contract_sha256 is the
    -- document. Both are copied from the signed row at send time so the record stays checkable
    -- even if a later reader cannot resolve the contract.
    contract_version TEXT,
    contract_sha256  TEXT,

    -- 'capture' (sandbox, written to public.email_outbox) | 'smtp' (a live relay send).
    -- 'killed' is deliberately NOT a value here: a kill-switched send never claims a row at all,
    -- so it can still be delivered by a later sweep.
    transport        TEXT NOT NULL,

    -- 'claimed' → a send is in flight; 'sent' → the transport accepted it; 'failed' → the attempt
    -- did not land and the row is re-claimable. Only 'sent' suppresses a further attempt.
    outcome          TEXT NOT NULL DEFAULT 'claimed',
    attempts         INTEGER NOT NULL DEFAULT 1,

    -- Short, secret-free failure class (e.g. 'SMTP send failed (TimeoutError)'). Never a body,
    -- never a credential.
    error            TEXT,

    -- The operator who asked for a re-send. NULL for the automatic lanes — an automatic notice has
    -- no human author and inventing one would make the audit trail lie.
    requested_by     TEXT,

    claimed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at          TIMESTAMPTZ,

    CONSTRAINT contract_notice_log_kind_chk
        CHECK (notice_kind IN ('agreement_copy', 'version_notice', 'agreement_copy_resend')),
    CONSTRAINT contract_notice_log_transport_chk
        CHECK (transport IN ('capture', 'smtp')),
    CONSTRAINT contract_notice_log_outcome_chk
        CHECK (outcome IN ('claimed', 'sent', 'failed')),

    -- THE exactly-once contract.
    CONSTRAINT contract_notice_log_once
        UNIQUE (contract_id, notice_kind, notice_ref)
);

-- The sweeps ask "which live signatures have no SENT row of this kind": an index on the lookup
-- key, and one on the audit read (newest first).
CREATE INDEX IF NOT EXISTS idx_contract_notice_log_lookup
    ON public.contract_notice_log (notice_kind, notice_ref, contract_id);
CREATE INDEX IF NOT EXISTS idx_contract_notice_log_claimed
    ON public.contract_notice_log (claimed_at DESC);

COMMENT ON TABLE public.contract_notice_log IS
    'Delivery record + exactly-once arbiter for CONTRACTUAL notices (control-plane public.*). Two '
    'duties: (1) Consumer Protection Act, 2002 (Ontario) s.39 with O. Reg. 17/05 s.33 — deliver a '
    'written copy of an internet agreement within 15 days, in a manner that lets the supplier '
    'prove receipt; (2) section 8 of the Subscription Agreement — 30 days'' notice of a MATERIAL '
    'new version. UNIQUE (contract_id, notice_kind, notice_ref) IS the exactly-once contract: the '
    'sender does INSERT ... ON CONFLICT DO UPDATE WHERE <retryable> and treats zero affected rows '
    'as "already delivered — send nothing". A FAILED attempt is KEPT (outcome=''failed'') rather '
    'than deleted, because on this table the row is the evidence. Never holds agreement text — the '
    'signed copy lives once, in public.signed_contracts.contract_snapshot. Written by '
    'saas/contract_notices.py.';
COMMENT ON COLUMN public.contract_notice_log.notice_ref IS
    'Disambiguator within (contract_id, notice_kind): '''' for the single statutory agreement_copy, '
    'the NEW template version for a version_notice, and a fresh uuid for an operator resend (so a '
    'resend is always recorded and never suppressed). Empty string, never NULL — NULLs do not '
    'conflict in a UNIQUE index and would silently disable the exactly-once guarantee.';
COMMENT ON COLUMN public.contract_notice_log.to_address IS
    'signed_contracts.signer_email — the immutable address given WITH the agreement (O. Reg. 17/05 '
    's.33(3)1: "an e-mail address that the consumer has given the supplier for providing '
    'information related to the agreement"). Deliberately NOT the login email, which is '
    'self-mutable and can be re-claimed by a different person after an offboarding.';
COMMENT ON COLUMN public.contract_notice_log.outcome IS
    'claimed = in flight; sent = the transport accepted it; failed = the attempt did not land and '
    'the row is re-claimable. ONLY ''sent'' suppresses a further attempt, so a transient mail '
    'failure can never consume a customer''s statutory delivery.';
