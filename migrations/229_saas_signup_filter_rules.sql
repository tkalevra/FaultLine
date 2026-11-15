-- 229_saas_signup_filter_rules.sql — the GLOBAL signup spam filter: operator-editable rules
-- applied to EVERY signup on BOTH doors — CLOSED-SOURCE SaaS.
--
-- THE CONTRACT: public.signup_filter_rules, read by saas/signup_filter.evaluate(email) from
--   POST /me/signup      (saas/user_console.py)
--   POST /signup/team    (saas/tenant_signup.py)
-- Operator surface: GET/POST/DELETE /admin/signup-filter/rules (require_operator).
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- WHY THIS IS A RULE VOCABULARY AND *NOT* AN OPERATOR-SUPPLIED REGEX
-- ════════════════════════════════════════════════════════════════════════════════════════
-- The ask was "a regex search for words or address types, like a spam filter". Storing raw
-- operator regex and running it on the signup path is a self-inflicted denial of service:
--
--   1. CATASTROPHIC BACKTRACKING. A pattern as innocent-looking as (a+)+$ takes exponential
--      time on a crafted input. The operator typing it is not an attacker — they are an admin
--      who wanted to filter "aaa" domains — and the blast radius is the whole signup funnel.
--   2. PYTHON CANNOT BOUND IT. CPython's `re` has no match timeout, and the matching loop does
--      not return to the interpreter, so it is NOT interruptible: a watchdog thread cannot kill
--      it, `signal.alarm` only works on the main thread of the main interpreter (never true
--      under a uvicorn worker), and the runaway thread would leak permanently. There is no
--      honest in-process mitigation, only the appearance of one.
--   3. STATIC SAFETY ANALYSIS IS NOT A THING WE CAN SHIP. "Reject dangerous patterns at save
--      time" sounds like the fix; in practice detecting super-linear regex is undecidable in
--      general and every heuristic both over- and under-rejects. Shipping one would mean
--      claiming a guarantee we cannot keep.
--
-- So the vocabulary below is deliberately restricted to operations that are PROVABLY LINEAR in
-- the length of the address — equality, prefix, and substring — with no backtracking to be had:
--
--   domain_equals   the domain is exactly this          ('spam-r-us.tk')
--   domain_suffix   the domain is this or a subdomain   ('.tk' → 'x.tk','a.b.tk'; 'mail.ru')
--   local_prefix    the local part starts with this     ('test', 'noreply')
--   local_contains  the local part contains this        ('casino')
--   email_contains  the whole address contains this     ('viagra')
--
-- That covers both things the ask actually named — "words" (local_contains / email_contains) and
-- "address types" (domain_equals / domain_suffix) — at zero DoS cost. The trade is expressive
-- power we could not have run safely anyway. saas/signup_filter.py REJECTS anything that looks
-- like a regex at SAVE time with a message naming the equivalent kind, so the operator is
-- redirected rather than silently disappointed.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- FAILURE POSTURE — the asymmetry is the whole design
-- ════════════════════════════════════════════════════════════════════════════════════════
--   A MATCHING BLOCK RULE  → fails CLOSED for the applicant (signup refused).
--   A BROKEN RULE STORE    → fails OPEN for everyone (signup proceeds). An unreadable table, a
--                            malformed stored row, a cache miss during a DB blip: none of these
--                            may take the funnel down. A spam filter that stops signups when it
--                            breaks is worse than no spam filter. Individual malformed rows are
--                            SKIPPED, not fatal.
--   action='allow'         → an ALLOW rule wins over every BLOCK rule and short-circuits. This
--                            is the escape hatch for a too-broad block: when 'domain_suffix .co'
--                            accidentally catches a real customer, the operator adds
--                            'domain_equals goodcustomer.co' as an allow instead of having to
--                            find and unpick the offending block under time pressure.
--
-- OTHER BOUNDS (enforced in saas/signup_filter.py, restated here so the table's limits are
-- documented where the schema lives):
--   * pattern length      ≤ 200 chars, validated at SAVE time (400) — never at signup time;
--   * evaluated address   ≤ 254 chars (RFC 5321 maximum) — a longer string is not a filter
--                         decision at all, it is an invalid address;
--   * active rules        ≤ 500 — a cap, so the linear scan stays bounded no matter what;
--   * rules are cached in-process for a few seconds, so signup does not query per attempt.
--
-- Control-plane public.* ONLY — never seeded into a tenant schema. Idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS public.signup_filter_rules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- WHICH linear operation to apply. The CHECK is the authority: an unknown kind can never be
    -- stored, so the evaluator never has to decide what to do with one.
    kind         TEXT NOT NULL,
    -- The needle, stored already normalized (trimmed + lowercased) by saas/signup_filter.py.
    pattern      TEXT NOT NULL,
    -- 'block' refuses the signup; 'allow' exempts the address and short-circuits (see above).
    action       TEXT NOT NULL DEFAULT 'block',
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    note         TEXT,
    -- Cheap "is this rule doing anything?" telemetry so an operator can retire dead rules and,
    -- more importantly, notice a rule that is matching far more than they expected.
    hit_count    BIGINT NOT NULL DEFAULT 0,
    last_hit_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by   TEXT
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'signup_filter_rules_kind_check'
    ) THEN
        ALTER TABLE public.signup_filter_rules
            ADD CONSTRAINT signup_filter_rules_kind_check
            CHECK (kind IN ('domain_equals', 'domain_suffix', 'local_prefix',
                            'local_contains', 'email_contains'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'signup_filter_rules_action_check'
    ) THEN
        ALTER TABLE public.signup_filter_rules
            ADD CONSTRAINT signup_filter_rules_action_check
            CHECK (action IN ('block', 'allow'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'signup_filter_rules_pattern_check'
    ) THEN
        -- Length is bounded at the DB too, not only in the app: the cap is a safety property
        -- of the signup path, so it should survive somebody inserting by hand.
        ALTER TABLE public.signup_filter_rules
            ADD CONSTRAINT signup_filter_rules_pattern_check
            CHECK (length(pattern) BETWEEN 1 AND 200);
    END IF;
END $$;

-- One rule per (kind, pattern, action) — re-adding an existing rule is an idempotent no-op
-- rather than a silently duplicated scan entry.
CREATE UNIQUE INDEX IF NOT EXISTS uq_signup_filter_rules
    ON public.signup_filter_rules (kind, pattern, action);

-- The evaluator loads only enabled rules; the table is small but the partial index keeps the
-- hot read off any disabled backlog an operator has parked.
CREATE INDEX IF NOT EXISTS idx_signup_filter_rules_enabled
    ON public.signup_filter_rules (kind)
    WHERE enabled;

COMMENT ON TABLE public.signup_filter_rules IS
    'GLOBAL signup spam filter (control-plane public.*). Operator-editable rules evaluated by '
    'saas/signup_filter.evaluate() on BOTH signup doors before any DB write. Deliberately a '
    'restricted, PROVABLY LINEAR rule vocabulary (equality / prefix / substring) rather than '
    'operator-supplied regex: CPython''s re has no match timeout and is not interruptible, so a '
    'pathological operator pattern would hang the signup path for everyone with no in-process '
    'mitigation available. A matching block rule fails CLOSED for the applicant; an unreadable '
    'or malformed rule store fails OPEN for everyone. action=''allow'' short-circuits over every '
    'block rule (the escape hatch for a too-broad block).';

COMMENT ON COLUMN public.signup_filter_rules.kind IS
    'domain_equals | domain_suffix | local_prefix | local_contains | email_contains. All five '
    'are O(len(address)) string operations with no backtracking. The CHECK is the authority — '
    'an unknown kind cannot be stored, so the evaluator never meets one.';

COMMENT ON COLUMN public.signup_filter_rules.action IS
    '''block'' refuses the signup (fail-closed for the applicant); ''allow'' exempts the address '
    'and short-circuits the whole evaluation, overriding every block rule. Allow exists so a '
    'too-broad block can be corrected by ADDING a rule under time pressure rather than by '
    'hunting down and unpicking the offending one.';
