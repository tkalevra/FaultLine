-- 304_deleted_email_reusable.sql — A SOFT-DELETED EMAIL IS REUSABLE (BUG-07) — CLOSED-SOURCE SaaS.
--
-- THE WOUND THIS CLOSES (BUG-07, P2): the sanctioned moderation DELETE is a SOFT delete
-- (status -> 'disabled' + deleted_at tombstone, migration 228), but signup uniqueness was
-- STATUS-AGNOSTIC with no deleted_at carve-out: user_console._email_taken matched tombstoned
-- rows, and the RAW UNIQUE(email) plus migration 231's canonical unique index made even a
-- direct INSERT impossible. A deleted person could never log in (status != active) and never
-- re-sign up — the mailbox was held hostage forever, and the only exit was a manual hard
-- delete on prod (the ct8ball incident). That is the opposite of a GDPR erasure posture.
--
-- POSTURE (chosen deliberately, documented in the BUG-07 record): DELETED = RELEASED.
--   * The unique indexes on email / email_canonical become PARTIAL: WHERE deleted_at IS NULL.
--   * user_console._email_taken filters deleted_at IS NULL on both arms (code change, same lane).
--   * moderation.restore_subscriber refuses (409) when the address was re-registered meanwhile.
--
-- WHY THIS DOES NOT OPEN BAN-EVASION — the property that makes posture (a) safe:
--   A BAN was NEVER enforced by the roster row. ban_subscriber FIRST writes
--   public.identity_bans (canonical email, lifted_at IS NULL) and BOTH signup doors call
--   moderation.signup_ban_check BEFORE the duplicate pre-check — so a banned identity is
--   refused at the door regardless of what the roster's unique indexes permit. Freeing a
--   tombstoned row's email (and a banned row IS tombstoned: ban stamps deleted_at too) changes
--   nothing about the ban's teeth.
--
-- WHY PENDING/DENIED APPLICANTS STILL HOLD THE MAILBOX: only the moderation DELETE/BAN verbs
-- stamp deleted_at. A held (pending_approval) or denied applicant row carries NO tombstone,
-- so it still claims the address exactly as migration 231 intended — a denied applicant
-- cannot re-apply around the decision.
--
-- RELAXING a unique constraint cannot fail on existing data (it only ever permits more), so
-- unlike migration 231 there is no duplicate pre-check and no failure mode: tombstoned rows
-- keep their values, they simply stop colliding with new signups.
--
-- Only the INDIVIDUAL door is affected: dashboard_users and tenant_signup_requests have no
-- soft-delete tombstone (their uniqueness scopes — global and pending-only respectively — are
-- unchanged).
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.

-- ── (1) the RAW address: table constraint (migration 149) → partial unique index ──────────
-- ALTER TABLE ... DROP CONSTRAINT IF EXISTS cannot run inside a DO block conditionally on
-- catalog checks in a way that survives a half-applied earlier run cleanly; plain statements
-- with IF EXISTS are idempotent on their own.

ALTER TABLE public.individual_users
    DROP CONSTRAINT IF EXISTS individual_users_email_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_individual_users_email_live
    ON public.individual_users (email)
    WHERE deleted_at IS NULL;

-- ── (2) the CANONICAL identity: migration 231's index gets the same partial predicate ─────
-- The predicate change REQUIRES a drop+recreate — CREATE ... IF NOT EXISTS on the same name
-- would silently keep the old, unrestricted predicate.

DROP INDEX IF EXISTS public.uq_individual_users_email_canonical;

CREATE UNIQUE INDEX IF NOT EXISTS uq_individual_users_email_canonical
    ON public.individual_users (email_canonical)
    WHERE email_canonical IS NOT NULL AND deleted_at IS NULL;

-- (3) documentation on the columns, so the next reader of \d does not re-derive 231's posture.

DO $$
BEGIN
    EXECUTE $c$
        COMMENT ON COLUMN public.individual_users.email IS
            'Login id (globally unique, lowercased). Since migration 304 uniqueness is PARTIAL: '
            'WHERE deleted_at IS NULL — a soft-deleted (tombstoned) row releases the address for '
            're-signup (BUG-07, GDPR posture). A banned row is also tombstoned, but the BAN is '
            'enforced by public.identity_bans at both signup doors, not by this index. '
            'Authentication still resolves on this RAW address, exactly as typed.'
    $c$;
    EXECUTE $c$
        COMMENT ON COLUMN public.individual_users.email_canonical IS
            'DERIVED per-provider canonical form of email (saas/email_identity.py); the SIGNUP '
            'UNIQUENESS key since migration 231 and the ban-match key. Since migration 304 the '
            'unique index is PARTIAL: WHERE deleted_at IS NULL AND email_canonical IS NOT NULL '
            '— a tombstoned row releases the canonical identity too. NEVER a credential; '
            'authentication resolves on the RAW address.'
    $c$;
END $$;
