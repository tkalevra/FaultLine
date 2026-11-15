-- 231_canonical_signup_identity.sql — SIGNUP UNIQUENESS ON THE CANONICAL MAILBOX — CLOSED-SOURCE SaaS.
--
-- THE HOLE THIS CLOSES: signup uniqueness keyed on the RAW address, so ``a+1@gmail.com`` and
-- ``a+2@gmail.com`` were two accounts. They are one mailbox and one human. Press '+' once and you
-- have another free trial; press it a hundred times and you have a farm. Migration 228 already
-- derives the per-provider canonical mailbox (``email_canonical``, saas/email_identity.py) but
-- deliberately left it NON-unique and read only by the ban matcher. This migration promotes it to
-- the uniqueness key at the DOOR, in the database, on every table a signup writes.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- WHAT DOES *NOT* CHANGE — READ THIS BEFORE TOUCHING ANY OF IT
-- ════════════════════════════════════════════════════════════════════════════════════════
-- LOGIN STILL RESOLVES ON THE RAW ADDRESS. Nobody's stored ``email`` / ``username`` is rewritten
-- by this migration or by the code that accompanies it, and NO authentication path is switched to
-- the canonical form. A person who registered ``sam+faultline@gmail.com`` signs in with exactly
-- that string, forever. People save credentials to a password manager; silently changing someone's
-- username locks them out of their own vault entry for a benefit they never asked for. The
-- canonical form is an IDENTITY KEY for "may this signup happen", never a credential.
--
--   uniqueness (NEW)  →  email_canonical   "is this mailbox already taken?"
--   authentication    →  email / username  "who is typing, exactly as they typed it?"
--
-- CANONICALISATION IS PER PROVIDER AND STAYS THAT WAY (migration 228 / saas/email_identity.py):
--   GMAIL ONLY   lowercase + strip dots from the local part + strip a '+suffix'.
--   EVERY OTHER  lowercase + strip a '+suffix' ONLY. DOTS ARE PRESERVED, because
--                john.smith@corp.com and johnsmith@corp.com are plausibly TWO DIFFERENT
--                EMPLOYEES. Folding those together would refuse signup to somebody who did
--                nothing wrong, on the strength of a quirk of a provider they do not use.
--                Under a UNIQUE index that false positive is no longer a mis-matched ban — it is
--                a person who cannot create an account and never finds out why. The guard matters
--                MORE now than it did in 228, not less.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- THE THREE TABLES A SIGNUP WRITES — all three get the same key
-- ════════════════════════════════════════════════════════════════════════════════════════
--   public.individual_users        POST /me/signup      (saas/user_console.py)
--   public.tenant_signup_requests  POST /signup/team, approval ON  (the HELD queue, migration 226)
--   public.dashboard_users         POST /signup/team, approval OFF (the OWNER login)
--
-- An application-level pre-check is NOT enough and that is the whole reason this file exists: two
-- simultaneous requests can both pass the same SELECT and both proceed to INSERT. The unique index
-- is the authority; the pre-check exists only to turn the second one into a friendly 409 instead
-- of an exception. The app path handles the integrity error and returns the SAME message either
-- way, so the race and the ordinary case are indistinguishable to the user.
--
-- dashboard_users is constrained GLOBALLY (every row, operator and tenant alike), mirroring the
-- global UNIQUE(username) that has always been on that table. That is a real tightening: two
-- logins deliberately sharing one mailbox via '+tags' are no longer possible anywhere in the
-- dashboard realm. If such rows exist today this migration NAMES them and refuses (see below)
-- rather than picking a winner.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- THE HAZARD: DUPLICATES ALREADY IN THE TABLE — and why this migration REFUSES rather than fixes
-- ════════════════════════════════════════════════════════════════════════════════════════
-- Adding UNIQUE to a column that already contains duplicates fails. Three options were available:
--
--   (a) CREATE INDEX CONCURRENTLY.  REJECTED. It solves a lock-duration problem this table does
--       not have (individual_users/dashboard_users are login rosters — thousands of rows, and
--       migrations run at container start before uvicorn binds), and it introduces a WORSE one:
--       CONCURRENTLY cannot run inside a transaction or a DO block, so it cannot be gated on the
--       duplicate pre-check below; and when it fails it leaves an INVALID index behind, which
--       ``CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS`` on the next boot then SILENTLY SKIPS.
--       The deployment would report a clean migration run while uniqueness was never enforced and
--       the index was not even usable for queries. That is the exact failure mode (a silently
--       failed constraint, dBug-074) the entrypoint's error summary was added to stop.
--
--   (b) QUARANTINE / AUTO-MERGE / AUTO-DELETE the duplicate rows.  REJECTED, emphatically. Which
--       of ``a+1@gmail.com`` and ``a+2@gmail.com`` is the real account is a question about a
--       human being — which one has the memory, the seat, the subscription, the data they care
--       about. A migration cannot know, runs unattended at 3am, and cannot be undone. Deleting or
--       merging user rows is not a migration's job.
--
--   (c) FAIL LOUDLY, NAMING THE CONFLICTS, AND CHANGE NOTHING.  ← CHOSEN.
--       The pre-check counts the colliding canonical identities and RAISE EXCEPTIONs with the
--       first ten of them plus their row counts. Nothing is created, nothing is destroyed, the
--       app still boots (the entrypoint runs every migration and continues past errors, then
--       prints a summary naming this file), and an operator gets an actionable list. Re-run the
--       migration after the cleanup and it applies. Each table is checked in its OWN block, so a
--       dirty dashboard_users does not block the individual_users constraint.
--
--       DEGRADED-BUT-SAFE POSTURE while duplicates exist: the index is absent, so the DATABASE is
--       not enforcing canonical uniqueness — but the application pre-check on both doors already
--       is, so the farm is closed to everything except a genuine concurrent race. Uniqueness on
--       the RAW address (the pre-existing UNIQUE constraints) is untouched throughout.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- NULLs, AND THE ONE-SHOT BACKFILL
-- ════════════════════════════════════════════════════════════════════════════════════════
-- Migration 228 added ``individual_users.email_canonical`` but DID NOT BACKFILL IT — by design:
-- "the canonical form is defined by Python, and encoding a second (drifting) definition in SQL is
-- how the two disagree" (228, and it is right). Every row written since 228 carries the value;
-- every row older than 228 is NULL. A UNIQUE index treats NULLs as DISTINCT, so those legacy rows
-- would be unprotected: an attacker could still farm around a pre-228 account.
--
-- So this migration backfills — ONCE, and only over the subset where the SQL expression is
-- PROVABLY IDENTICAL to saas/email_identity.canonical_email(). ``public._mig231_canonical()``
-- below returns NULL (→ "leave this row alone") for every input where the two could differ:
--
--     * non-ASCII / whitespace anywhere            (lower() locale behaviour vs str.lower())
--     * a '"' anywhere                             (Python leaves a quoted local part untouched)
--     * anything but exactly one '@'               (Python splits on the LAST one)
--     * longer than 254 chars, empty local/domain, domain without a dot   (Python → None)
--     * a '+' in the local part of a NON-Gmail address — the plus-stripping rule there is
--       OVERRIDABLE per deployment by SAAS_EMAIL_NO_PLUS_DOMAINS, an environment variable SQL
--       cannot see. Guessing would write a WRONG identity key into a UNIQUE column, i.e. refuse
--       a stranger's signup for no reason. Left NULL instead.
--
-- What remains is (i) every Gmail/googlemail address, where dot+plus folding is documented Google
-- behaviour and env-independent, and (ii) every non-Gmail address with no '+', whose canonical
-- form is just its lowercase self. Both are exactly what Python produces. The helper is DROPPED
-- at the end of this file so no second definition survives it; Python remains the authority for
-- every write.
--
-- The backfill is a derived column only — it never touches ``email``/``username`` and never
-- removes a row. It CAN surface duplicates that were always there but invisible; that is the
-- point, and it is why the duplicate check runs AFTER it.
--
-- Rows still NULL after the backfill are reported by NOTICE. They keep exactly today's behaviour
-- (uniqueness on the raw address); the app re-derives their canonical form on the next write.
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.

-- ── (0) the canonical helper (one-shot, dropped at the bottom of this file) ──────────────

CREATE OR REPLACE FUNCTION public._mig231_canonical(raw TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
AS $fn$
DECLARE
    v   TEXT;
    lp  TEXT;
    dom TEXT;
BEGIN
    IF raw IS NULL THEN
        RETURN NULL;
    END IF;
    v := btrim(raw);
    -- Every guard below means "the Python answer and the SQL answer might differ here, so do not
    -- write anything". NULL is always the safe return: it leaves the row exactly as it is.
    IF v = '' OR length(v) > 254 THEN                     -- RFC 5321 §4.5.3.1.3
        RETURN NULL;
    END IF;
    IF v !~ '^[!-~]+$' THEN                               -- printable ASCII only, no whitespace
        RETURN NULL;
    END IF;
    IF position('"' IN v) > 0 THEN                        -- quoted local part: Python folds nothing
        RETURN NULL;
    END IF;
    IF length(v) - length(replace(v, '@', '')) <> 1 THEN  -- exactly one '@'
        RETURN NULL;
    END IF;
    lp  := lower(split_part(v, '@', 1));
    dom := rtrim(lower(split_part(v, '@', 2)), '.');
    IF lp = '' OR dom = '' OR position('.' IN dom) = 0 THEN
        RETURN NULL;
    END IF;
    IF dom IN ('gmail.com', 'googlemail.com') THEN
        -- Documented Google behaviour, and independent of any deployment setting.
        dom := 'gmail.com';
        lp  := replace(split_part(lp, '+', 1), '.', '');
    ELSIF position('+' IN lp) > 0 THEN
        -- SAAS_EMAIL_NO_PLUS_DOMAINS may make '+' literal here. SQL cannot know. Refuse.
        RETURN NULL;
    END IF;
    IF lp = '' THEN                                       -- '+tag@x.com' / '.@gmail.com'
        RETURN NULL;
    END IF;
    RETURN lp || '@' || dom;
END;
$fn$;

-- ── (1) the canonical column on the two tables that did not have one ─────────────────────
-- individual_users.email_canonical already exists (migration 228).

DO $$
BEGIN
    IF to_regclass('public.dashboard_users') IS NOT NULL THEN
        ALTER TABLE public.dashboard_users ADD COLUMN IF NOT EXISTS email_canonical TEXT;
    END IF;
    IF to_regclass('public.tenant_signup_requests') IS NOT NULL THEN
        ALTER TABLE public.tenant_signup_requests ADD COLUMN IF NOT EXISTS email_canonical TEXT;
    END IF;
END $$;

-- ── (2) one-shot conservative backfill (derived column only — see the header) ────────────

DO $$
DECLARE
    _filled INTEGER;
    _left   INTEGER;
BEGIN
    UPDATE public.individual_users
       SET email_canonical = public._mig231_canonical(email)
     WHERE email_canonical IS NULL
       AND public._mig231_canonical(email) IS NOT NULL;
    GET DIAGNOSTICS _filled = ROW_COUNT;
    SELECT count(*) INTO _left
      FROM public.individual_users WHERE email_canonical IS NULL;
    RAISE NOTICE 'migration 231: individual_users.email_canonical — backfilled % row(s); % row(s) '
                 'left NULL (not provably canonicalisable in SQL; the app derives them on next '
                 'write and they keep raw-address uniqueness meanwhile)', _filled, _left;

    IF to_regclass('public.dashboard_users') IS NOT NULL THEN
        UPDATE public.dashboard_users
           SET email_canonical = public._mig231_canonical(username)
         WHERE email_canonical IS NULL
           AND public._mig231_canonical(username) IS NOT NULL;
        GET DIAGNOSTICS _filled = ROW_COUNT;
        SELECT count(*) INTO _left
          FROM public.dashboard_users WHERE email_canonical IS NULL;
        RAISE NOTICE 'migration 231: dashboard_users.email_canonical — backfilled % row(s); % '
                     'row(s) left NULL (operator logins whose username is not an email address '
                     'land here on purpose)', _filled, _left;
    END IF;

    IF to_regclass('public.tenant_signup_requests') IS NOT NULL THEN
        UPDATE public.tenant_signup_requests
           SET email_canonical = public._mig231_canonical(email)
         WHERE email_canonical IS NULL
           AND public._mig231_canonical(email) IS NOT NULL;
        GET DIAGNOSTICS _filled = ROW_COUNT;
        RAISE NOTICE 'migration 231: tenant_signup_requests.email_canonical — backfilled % row(s)',
                     _filled;
    END IF;
END $$;

-- ── (3) individual_users — UNIQUE on the canonical mailbox (/me/signup) ──────────────────
-- Own block: a dirty dashboard_users must not stop this one from applying.

DO $$
DECLARE
    _groups INTEGER;
    _rows   INTEGER;
    _sample TEXT;
BEGIN
    SELECT count(*), COALESCE(sum(d.n), 0) INTO _groups, _rows
      FROM (SELECT count(*) AS n
              FROM public.individual_users
             WHERE email_canonical IS NOT NULL
             GROUP BY email_canonical
            HAVING count(*) > 1) d;

    IF _groups > 0 THEN
        SELECT string_agg(t.email_canonical || ' (' || t.n || ' rows: ' || t.ids || ')',
                          E'\n    ' ORDER BY t.n DESC, t.email_canonical)
          INTO _sample
          FROM (SELECT email_canonical,
                       count(*) AS n,
                       string_agg(id::text, ', ' ORDER BY created_at) AS ids
                  FROM public.individual_users
                 WHERE email_canonical IS NOT NULL
                 GROUP BY email_canonical
                HAVING count(*) > 1
                 ORDER BY count(*) DESC, email_canonical
                 LIMIT 10) t;
        RAISE EXCEPTION
            'migration 231: public.individual_users already holds % canonical identities shared '
            'by % rows. NOTHING has been created, changed or deleted. Decide per person which '
            'login survives, then re-run this migration.%    Conflicts (first 10):%    %',
            _groups, _rows, E'\n', E'\n', _sample
            USING HINT = 'A migration must not pick which of two real accounts is the real person '
                         '— that is unrecoverable. Merge or retire them from the operator console, '
                         'then re-run. Signup uniqueness on the canonical form is ALREADY enforced '
                         'in the application on both doors; only the concurrent-race guard is '
                         'missing until this index exists.';
    END IF;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_individual_users_email_canonical
        ON public.individual_users (email_canonical)
        WHERE email_canonical IS NOT NULL;
END $$;

-- NOTE: migration 228's non-unique idx_individual_users_email_canonical is left in place. It is
-- redundant with the index above, but 228 re-creates it on every boot (the entrypoint runs every
-- migration in order), so dropping it here would only produce a create/drop ping-pong on each
-- container start. Harmless duplication beats churn.

-- ── (4) dashboard_users — UNIQUE on the canonical mailbox (/signup/team, approval OFF) ───

DO $$
DECLARE
    _groups INTEGER;
    _rows   INTEGER;
    _sample TEXT;
BEGIN
    IF to_regclass('public.dashboard_users') IS NULL THEN
        RETURN;
    END IF;

    SELECT count(*), COALESCE(sum(d.n), 0) INTO _groups, _rows
      FROM (SELECT count(*) AS n
              FROM public.dashboard_users
             WHERE email_canonical IS NOT NULL
             GROUP BY email_canonical
            HAVING count(*) > 1) d;

    IF _groups > 0 THEN
        SELECT string_agg(t.email_canonical || ' (' || t.n || ' logins: ' || t.names || ')',
                          E'\n    ' ORDER BY t.n DESC, t.email_canonical)
          INTO _sample
          FROM (SELECT email_canonical,
                       count(*) AS n,
                       string_agg(username, ', ' ORDER BY username) AS names
                  FROM public.dashboard_users
                 WHERE email_canonical IS NOT NULL
                 GROUP BY email_canonical
                HAVING count(*) > 1
                 ORDER BY count(*) DESC, email_canonical
                 LIMIT 10) t;
        RAISE EXCEPTION
            'migration 231: public.dashboard_users already holds % canonical identities shared '
            'by % logins. NOTHING has been created, changed or deleted.%    Conflicts (first '
            '10):%    %',
            _groups, _rows, E'\n', E'\n', _sample
            USING HINT = 'These are usually deliberate: a staff member holding sam@x.com and '
                         'sam+ops@x.com as two dashboard logins. One mailbox is now one login, '
                         'so retire or re-address the extras, then re-run. The individual_users '
                         'constraint applies independently and is unaffected by this.';
    END IF;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_dashboard_users_email_canonical
        ON public.dashboard_users (email_canonical)
        WHERE email_canonical IS NOT NULL;
END $$;

-- ── (5) tenant_signup_requests — UNIQUE among PENDING (/signup/team, approval ON) ────────
-- Scoped to status='pending', exactly mirroring uq_tenant_signup_requests_pending_email
-- (migration 226): a denied/expired application must not permanently burn the mailbox.

DO $$
DECLARE
    _groups INTEGER;
    _sample TEXT;
BEGIN
    IF to_regclass('public.tenant_signup_requests') IS NULL THEN
        RETURN;
    END IF;

    SELECT count(*) INTO _groups
      FROM (SELECT 1
              FROM public.tenant_signup_requests
             WHERE email_canonical IS NOT NULL AND status = 'pending'
             GROUP BY email_canonical
            HAVING count(*) > 1) d;

    IF _groups > 0 THEN
        SELECT string_agg(t.email_canonical || ' (' || t.n || ')', ', '
                          ORDER BY t.n DESC, t.email_canonical)
          INTO _sample
          FROM (SELECT email_canonical, count(*) AS n
                  FROM public.tenant_signup_requests
                 WHERE email_canonical IS NOT NULL AND status = 'pending'
                 GROUP BY email_canonical
                HAVING count(*) > 1
                 LIMIT 10) t;
        RAISE EXCEPTION
            'migration 231: public.tenant_signup_requests holds % canonical identities shared by '
            'more than one PENDING application: %. NOTHING has been created, changed or deleted.',
            _groups, _sample
            USING HINT = 'Deny the duplicate applications in the operator queue (they are held '
                         'applications, not accounts — denying one destroys nothing), then re-run.';
    END IF;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_signup_requests_pending_canonical
        ON public.tenant_signup_requests (email_canonical)
        WHERE email_canonical IS NOT NULL AND status = 'pending';
END $$;

-- ── (6) documentation on the columns ─────────────────────────────────────────────────────

COMMENT ON COLUMN public.individual_users.email_canonical IS
    'DERIVED per-provider canonical form of `email` (saas/email_identity.py). Since migration 231 '
    'this is the SIGNUP UNIQUENESS key (UNIQUE where NOT NULL) as well as the ban-match key. It is '
    'NEVER a credential and NEVER a substitute for `email`: authentication resolves on the RAW '
    'address, exactly as the person typed and saved it. Gmail/googlemail: lowercase + dots '
    'stripped + ''+suffix'' stripped. Every other domain: lowercase + ''+suffix'' stripped ONLY — '
    'dots are NOT stripped, because john.smith@corp.com and johnsmith@corp.com may be two '
    'different people and a UNIQUE index would turn that false positive into a lockout. NULL means '
    '"not derived yet" (callers recompute from the raw address; NULLs are distinct to the index).';

DO $$
BEGIN
    IF to_regclass('public.dashboard_users') IS NOT NULL THEN
        EXECUTE $c$
            COMMENT ON COLUMN public.dashboard_users.email_canonical IS
                'DERIVED canonical mailbox for `username` (saas/email_identity.py), UNIQUE where '
                'NOT NULL since migration 231: one mailbox, one dashboard login. Written by '
                'saas/dashboard_auth.create_dashboard_user / set_username. Authentication still '
                'resolves on the RAW `username`. NULL for operator logins whose username is not '
                'an email address, and for rows not yet re-derived.'
        $c$;
    END IF;
    IF to_regclass('public.tenant_signup_requests') IS NOT NULL THEN
        EXECUTE $c$
            COMMENT ON COLUMN public.tenant_signup_requests.email_canonical IS
                'DERIVED canonical mailbox for `email` (saas/email_identity.py), UNIQUE among '
                'status=''pending'' since migration 231 — the same scope as '
                'uq_tenant_signup_requests_pending_email, so a denied or expired application does '
                'not burn the mailbox forever.'
        $c$;
    END IF;
END $$;

-- ── (7) drop the one-shot helper — Python stays the ONLY definition of canonical_email ───

DROP FUNCTION IF EXISTS public._mig231_canonical(TEXT);
