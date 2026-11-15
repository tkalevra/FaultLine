-- 232_signup_ledger_canonical_key.sql — THE REPEAT-SIGNUP LADDER KEYS ON THE CANONICAL MAILBOX.
-- CLOSED-SOURCE SaaS layer. Documentation-only: NO schema change, NO data change.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- THE HOLE THIS CLOSES
-- ════════════════════════════════════════════════════════════════════════════════════════
-- public.signup_ledger (migration 179) is the 3-strikes repeat-signup memory that SURVIVES an
-- account purge. Its key is a SALTED SHA-256 of the normalised email — and until now
-- "normalised" meant trim + lowercase and NOTHING ELSE. So a@gmail.com, a+1@gmail.com and
-- a+2@gmail.com were THREE identities to the ladder: press '+' once and the counter starts over.
-- A 3-strikes ladder you can reset with a keystroke is not a control.
--
-- Migration 228 already derives the per-provider canonical mailbox (saas/email_identity.py) and
-- bans on it; migration 231 made it the SIGNUP UNIQUENESS key. This one brings the last of the
-- three onto the same identity: saas/signup_ledger.normalize_email now returns
-- email_identity.canonical_email(). One mailbox, one strike counter, one definition of "who".
--
--   GMAIL/googlemail  lowercase + strip dots from the local part + strip a '+suffix'.
--   EVERY OTHER       lowercase + strip a '+suffix' ONLY. DOTS ARE PRESERVED — john.smith@corp
--                     and johnsmith@corp are plausibly two different employees, and folding them
--                     would hang a stranger's strikes on an innocent person who then cannot sign
--                     up and never learns why. That guard is unchanged and load-bearing.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- WHY THERE IS NO DDL AND NO BACKFILL IN THIS FILE — the part worth reading
-- ════════════════════════════════════════════════════════════════════════════════════════
-- The key is sha256(salt || E'\\000' || normalized_email). ONLY the normalisation changed; the
-- hash function and the salt did not. Therefore:
--
--   * For every address whose canonical form EQUALS its lowercase form — every address with no
--     '+tag' and, on Gmail, no dots — the key is BIT-FOR-BIT IDENTICAL. Those rows, the
--     overwhelming majority of this table, are untouched and keep their full strike history.
--     There is nothing to migrate.
--
--   * For the tagged/dotted minority the key moves, and THE OLD ROWS CANNOT BE RE-KEYED. This
--     table holds a one-way salted hash and no plaintext — anywhere. That is the entire privacy
--     posture of migration 179: the purge reaper deletes the account and its email, and the
--     ledger deliberately retains nothing readable. There is therefore no input to
--     re-canonicalise. A backfill is not "expensive" or "risky"; it is IMPOSSIBLE, and any SQL
--     here claiming to do one would be a lie. (Nor can the salt help: it lives in an env secret
--     — SAAS_SIGNUP_LEDGER_SALT / SAAS_SESSION_SECRET — that SQL cannot see, so this file could
--     not even recompute a hash from a plaintext it does not have.)
--
-- WHAT THE APPLICATION DOES INSTEAD (saas/signup_ledger.py, and this is the whole transition):
--   WRITES  go to the CANONICAL key only.
--   READS   sum the row at the CANONICAL key AND the row at the caller's own LEGACY
--           (lowercase-only) key. So a pre-232 row recorded against the exact literal address a
--           caller re-types still counts against them: NO EXISTING STRIKE HISTORY IS LOWERED by
--           this change. The two arms collapse to one row whenever the keys agree, and when they
--           differ the rows count disjoint sets of attempts (pre-232 bumps only reached the
--           legacy row, post-232 bumps only reach the canonical one), so the sum is the true
--           total rather than a double count.
--
-- WHAT IS GENUINELY LOST, stated plainly rather than glossed: a pre-232 row keyed on a TAGGED
-- address stops matching when that person returns under a DIFFERENT tag. That history was
-- already inert — under the old key every tag had its own counter, so a variant row sat at 1 or 2
-- and blocked nothing. The only real loss is an address that reached 3 strikes under one literal
-- tagged string and comes back under another, which the old scheme let straight through anyway.
-- Losing that is strictly better than leaving the bypass open, and the ladder's purpose (make a
-- repeat free-trial cost something) tolerates it: the ladder is a friction control with a
-- fail-SAFE posture (an unreadable ledger ALLOWS), not an entitlement record.
--
-- Legacy rows are never written again. They serve only the exact-string read arm above and age
-- out on their own; they are hashes, not PII, and nothing else reads them.
--
-- ════════════════════════════════════════════════════════════════════════════════════════
-- SO WHY A MIGRATION FILE AT ALL
-- ════════════════════════════════════════════════════════════════════════════════════════
-- Because the COMMENTs migration 179 wrote on this table now describe a normalisation the code no
-- longer uses ("Salted SHA-256 hex of trim+lowercase email"). A schema comment that contradicts
-- the code is worse than no comment: the next person reads it, believes it, and reasons about
-- identity from a rule that has not been true since this commit. This file corrects the record
-- and nothing else.
--
-- Control-plane public.* ONLY. Idempotent; safe to re-run; changes no data.

-- public.signup_ledger is created unconditionally by migration 179, which the entrypoint runs
-- before this file, so these need no existence guard (contrast migration 231, whose tables are
-- genuinely optional). Plain COMMENT ON, exactly as 179 wrote them.

COMMENT ON TABLE public.signup_ledger IS
        'Repeat-signup memory that SURVIVES an individual account purge (control-plane public.*). '
        'Keyed by a SALTED SHA-256 HASH of the CANONICAL MAILBOX (migration 232) — NOT plaintext '
        '(privacy): the purge reaper deletes the account + all PII, this row persists so a '
        're-signup on the same mailbox still walks the 3-strikes ladder (attempt 1 = allowed, '
        '2 = allowed_cheeky, 3+ = rejected). Since migration 232 every ''+tag'' variant (and, on '
        'Gmail, every dotted variant) of one mailbox shares ONE row, so the ladder can no longer '
        'be reset by adding a tag. Rows written before 232 under the lowercase-only key are still '
        'CONSULTED (the reader sums the canonical key and the caller''s own legacy key) but are '
        'never written again. Written by saas/signup_ledger.py from saas/user_console.py::me_signup, '
        'saas/tenant_signup.py::team_signup and the purge reaper.';

COMMENT ON COLUMN public.signup_ledger.email_hash IS
        'Salted SHA-256 hex of the CANONICAL MAILBOX (saas/email_identity.canonical_email: '
        'lowercase, ''+suffix'' stripped, and on gmail/googlemail dots stripped too — dots are '
        'PRESERVED on every other domain, because john.smith@corp.com and johnsmith@corp.com may '
        'be two different people). Salt = env SAAS_SIGNUP_LEDGER_SALT (or SAAS_SESSION_SECRET). '
        'Never store or log the plaintext address. The value is UNCHANGED by migration 232 for '
        'every address whose canonical form equals its lowercase form (i.e. no ''+tag'', and no '
        'dots on Gmail) — that is most of this table. Rows for tagged/dotted addresses written '
        'before 232 keep their old key and CANNOT be re-keyed: this table holds a one-way hash '
        'and no plaintext, so there is nothing to re-derive from. saas/signup_ledger.py therefore '
        'reads BOTH the canonical key and the legacy lowercase-only key and sums them, so no '
        'existing strike history is lowered.';
