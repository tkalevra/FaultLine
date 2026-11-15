-- 228_saas_account_moderation.sql — ACCOUNT MODERATION: block / ban / soft-delete a subscriber,
-- plus the PER-PROVIDER canonical email identity the ban is matched on — CLOSED-SOURCE SaaS.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- THREE VERBS, THREE DISTINCT MEANINGS. Do not let them blur.
-- ════════════════════════════════════════════════════════════════════════════════════════
--   BLOCK   — the login is SUSPENDED. The row stays, the address stays claimed, every seat and
--             every byte of memory stays. It cannot authenticate (the /me readers filter
--             status='active'). Fully REVERSIBLE (Unblock → 'active'). A block says nothing
--             about SIGNUP: the same person may still create a *different* account. That is
--             deliberate — a block is a lever on ONE login, not on a human being.
--   DELETE  — the login leaves the ACTIVE roster and lands in "Deleted (historical)". This is a
--             SOFT delete (a tombstone: deleted_at/deleted_by/delete_reason + status='disabled'),
--             NOT a row removal, for three reasons:
--               (1) the operator explicitly asked for a HISTORICAL tab — a hard DELETE has no
--                   history to show;
--               (2) the address must STAY CLAIMED. public.individual_users.email is UNIQUE;
--                   removing the row hands the address back to whoever wants it, so a deleted
--                   person could re-register around the decision (exactly the reasoning already
--                   documented on signup_approvals.deny, which keeps a denied row as 'disabled');
--               (3) public.accounts CANNOT be hard-deleted casually anyway — account_ledger is
--                   append-only (migration 139) and only the authorized purge path (migration
--                   141, faultline.account_purge='on' inside accounts.delete_account) may
--                   cascade it away, and only from 'archived'. Soft-delete of the LOGIN keeps
--                   moderation entirely clear of that machinery.
--             The irreversible hard purge already exists and is UNCHANGED: archive the ACCOUNT
--             then DELETE /admin/accounts/{id} with the typed-slug confirm. Delete here is
--             REVERSIBLE (Restore).
--   BAN     — the PERSON must not come back. This is the only verb that is NOT a property of a
--             row: it is an entry in public.identity_bans keyed on the CANONICAL email, checked
--             at BOTH signup doors (POST /me/signup and POST /signup/team) before any DB write.
--             Banning a subscriber ALSO blocks and soft-deletes their login, so the ban is
--             visible on the roster as well as enforced at the door.
--
-- RELATION TO THE EXISTING LIFECYCLES — this EXTENDS, it does not fork:
--   public.accounts.status  (migration 133: active|suspended|locked|archived) is UNTOUCHED.
--       Tenant-account moderation stays exactly where it is: set_account_status → 'archived'
--       is Trash, delete_account is the purge. No new account status, no new state machine.
--   public.individual_users.status (migration 149/174: active|pending_approval|suspended|
--       disabled) is REUSED as-is — no new value, no CHECK change:
--         BLOCK   → 'suspended'  (the value migration 149 declared and nothing had yet used;
--                                 picking it keeps 'disabled' meaning what signup_approvals
--                                 already made it mean — "decided against", not "paused")
--         DELETE  → 'disabled' + deleted_at  (the tombstone is what makes it a delete; status
--                                 alone only says "cannot log in")
--         BAN     → 'disabled' + banned_at + deleted_at + an identity_bans row
--       Every reader that already filters status='active' therefore enforces all three with NO
--       auth change anywhere — fail-closed by construction, same as migration 174 chose.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- EMAIL CANONICALISATION (email_canonical) — the part that can lock out innocent people.
-- ════════════════════════════════════════════════════════════════════════════════════════
-- Derived by saas/email_identity.canonical_email(). The RAW address is NEVER overwritten:
-- `email` remains exactly what the person typed (delivery + display), `email_canonical` is a
-- SEPARATE derived column used ONLY for ban matching and duplicate-identity reporting.
--
--   ALWAYS      lowercase; strip a trailing dot from the domain; googlemail.com → gmail.com.
--   GMAIL ONLY  strip dots from the local part AND strip a '+suffix'.
--               (ct8ball+account1@gmail.com, ct8ball+account2@gmail.com, ct.8ball@gmail.com and
--                ct8ball@googlemail.com are ONE mailbox — Google documents this.)
--   EVERY OTHER strip a '+suffix' ONLY. Dots are NOT stripped.
--   DOMAIN      ── this is the false-positive guard and it is the whole point ──
--               john.smith@company.com and johnsmith@company.com are plausibly TWO DIFFERENT
--               EMPLOYEES. Collapsing them would refuse signup to an innocent person on the
--               strength of a Gmail-specific quirk. So dot-folding is confined to Gmail, where
--               it is documented behaviour, and nowhere else.
--   NOT DONE    '-' as a separator (some providers treat it like '+', many real local parts
--               simply contain a hyphen — guessing here breaks real addresses); any per-provider
--               table beyond Gmail; case-folding INSIDE a quoted local part ("a.b"@x.com is
--               returned untouched). Plus-stripping on non-Gmail domains is the one broad
--               assumption and it is OVERRIDABLE per deployment via the env allow-list
--               SAAS_EMAIL_NO_PLUS_DOMAINS (comma-separated domains where '+' is literal).
--
-- The column is NULLABLE and backfilled best-effort by the app, not by this migration: the
-- canonical form is defined by Python, and encoding a second (drifting) definition in SQL is
-- how the two disagree. A NULL simply means "not derived yet" and the ban check falls back to
-- computing it in-process from the raw address, so nothing depends on the backfill.
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.

-- ── (1) individual_users: canonical identity + the moderation tombstones ─────────────────

ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS email_canonical TEXT;

ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS blocked_at     TIMESTAMPTZ;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS blocked_by     TEXT;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS block_reason   TEXT;

ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS deleted_at     TIMESTAMPTZ;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS deleted_by     TEXT;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS delete_reason  TEXT;

ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS banned_at      TIMESTAMPTZ;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS banned_by      TEXT;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS ban_reason     TEXT;

COMMENT ON COLUMN public.individual_users.email_canonical IS
    'DERIVED per-provider canonical form of `email`, used ONLY for ban matching and duplicate-'
    'identity reporting. NEVER a substitute for `email`, which stays exactly as typed. Gmail/'
    'googlemail: lowercase + dots stripped + ''+suffix'' stripped. Every other domain: lowercase '
    '+ ''+suffix'' stripped ONLY — dots are NOT stripped, because john.smith@corp.com and '
    'johnsmith@corp.com may be two different people. Computed by saas/email_identity.py; NULL '
    'means "not derived yet" (callers recompute from the raw address).';

COMMENT ON COLUMN public.individual_users.blocked_at IS
    'When this login was BLOCKED (status→''suspended''): suspended, reversible, cannot '
    'authenticate, address still claimed, memory and seats untouched. NULL = not blocked. '
    'Cleared on unblock. saas_audit_log is the record of authority; this is state provenance.';
COMMENT ON COLUMN public.individual_users.deleted_at IS
    'SOFT-delete tombstone: the login has left the ACTIVE roster and appears under "Deleted '
    '(historical)". status is ''disabled'' so it cannot authenticate, and the row is KEPT so the '
    'UNIQUE email stays claimed (a deleted person cannot re-register around the decision) and '
    'the history survives. Reversible (Restore). This is NOT the irreversible purge — that is '
    'still accounts.delete_account on an ARCHIVED account (migrations 139/141).';
COMMENT ON COLUMN public.individual_users.banned_at IS
    'When the PERSON behind this login was BANNED. Distinct from blocked_at: a ban is enforced '
    'at SIGNUP against public.identity_bans(email_canonical) on BOTH doors, so the human cannot '
    'return under a new address at the same canonical mailbox. This column is the roster''s copy '
    'of that fact; public.identity_bans is the enforcement record.';

-- Ban matching reads by canonical address; duplicate-identity sweeps group by it. Partial (the
-- column is NULL until derived) and non-unique on purpose — TWO EXISTING ROWS MAY LEGITIMATELY
-- SHARE A CANONICAL FORM (they signed up as foo+a@gmail.com and foo+b@gmail.com before this
-- existed). A UNIQUE index here would fail the migration on live data and, worse, would make
-- canonicalisation a hard constraint on people who did nothing wrong. Uniqueness stays where it
-- always was: on the RAW `email`.
CREATE INDEX IF NOT EXISTS idx_individual_users_email_canonical
    ON public.individual_users (email_canonical)
    WHERE email_canonical IS NOT NULL;

-- The three moderation tabs are "WHERE <tombstone> IS NOT NULL" sweeps over a table that is
-- overwhelmingly active rows. Partial indexes keep them tiny.
CREATE INDEX IF NOT EXISTS idx_individual_users_deleted
    ON public.individual_users (deleted_at DESC)
    WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_individual_users_banned
    ON public.individual_users (banned_at DESC)
    WHERE banned_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_individual_users_blocked
    ON public.individual_users (blocked_at DESC)
    WHERE blocked_at IS NOT NULL;

-- ── (2) identity_bans = the PLATFORM-WIDE ban list (the "must not come back" record) ─────
-- One row per ban EVENT, not per person: lifting a ban sets lifted_at and KEEPS the row, so the
-- history of "banned, lifted, banned again" is legible. Only rows with lifted_at IS NULL are
-- enforced, and the partial UNIQUE index below is what makes "one ACTIVE ban per identity" a
-- database guarantee rather than an application convention.

CREATE TABLE IF NOT EXISTS public.identity_bans (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The canonical mailbox this ban covers (saas/email_identity.canonical_email). Lowercase.
    email_canonical  TEXT NOT NULL,
    -- The address exactly as the operator saw/typed it — display + provenance ONLY. Never
    -- matched against; matching is always canonical-to-canonical.
    email_raw        TEXT,
    reason           TEXT,
    -- The subscriber row this ban was raised from, when it came from a roster button (NULL when
    -- the operator banned a bare address pre-emptively). No FK: the ban must OUTLIVE the login
    -- row, which is the entire point of a ban.
    individual_id    UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by       TEXT,
    lifted_at        TIMESTAMPTZ,
    lifted_by        TEXT,
    lift_reason      TEXT
);

-- ONE active ban per identity; unlimited historical ones. A lifted row leaves the index, so the
-- same identity can be re-banned without touching history.
CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_bans_active
    ON public.identity_bans (email_canonical)
    WHERE lifted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_identity_bans_created
    ON public.identity_bans (created_at DESC);

COMMENT ON TABLE public.identity_bans IS
    'PLATFORM-WIDE signup ban list (control-plane public.*). One row per ban EVENT keyed on the '
    'per-provider CANONICAL email (saas/email_identity.py), enforced on BOTH signup doors — '
    'POST /me/signup (saas/user_console.py) and POST /signup/team (saas/tenant_signup.py) — '
    'before any DB write, via saas/moderation.is_banned(). Active iff lifted_at IS NULL; the '
    'partial UNIQUE index enforces one active ban per identity. Deliberately has NO FK to '
    'individual_users: a ban must survive the deletion of the row it came from. Operator-only '
    '(require_operator); every raise and lift is audited in saas_audit_log.';

COMMENT ON COLUMN public.identity_bans.email_canonical IS
    'The canonical mailbox. Gmail plus-tags and dots fold into ONE value here, so banning '
    'ct8ball@gmail.com also refuses ct8ball+throwaway@gmail.com and ct.8ball@googlemail.com. '
    'Non-Gmail domains fold ''+suffix'' only — dots are preserved, so banning john.smith@corp.com '
    'does NOT refuse johnsmith@corp.com (they may be different employees).';
