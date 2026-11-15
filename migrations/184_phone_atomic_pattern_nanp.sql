-- Migration 184: broaden the has_phone scalar_atomic pattern to match un-prefixed NANP
-- Date: 2026-07-16
--
-- WHY
-- ---
-- The seeded has_phone pattern (migration 060) was E.164-INTERNATIONAL-ONLY:
--     \+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}\b
-- It REQUIRES a leading '+', so a plain North-American number "519-555-0123"
-- (no country prefix — the overwhelmingly common form) never matched the atomic
-- detector. The number then fell through to the numeric deriver, which grabbed the
-- trailing group "0123" as an INTEGER scalar (leading zero stripped) → recall read
-- back "Your phone number is 123". The full number was shredded.
--
-- FIX: replace the pattern with an alternation — (existing international form) OR
-- (NANP grouped form "\(?\d{3}\)?[sep]\d{3}[sep]\d{4}", separators = space/dot/dash).
-- A leading (?<!\d) guard prevents matching the tail of a longer digit run. This is a
-- FORMAT GRAMMAR broadening (what atomic scalars ARE) — subject-agnostic, deterministic,
-- no domain literals. Verified NOT to match ISO dates (4-2-2), dotted IPv4, bare years,
-- or bare digit runs; DOES match "519-555-0123", "+1 (519) 555-0123", "519.555.0123",
-- "+44 20 7946 0958".
--
-- Mirrors the _BOOTSTRAP fallback in src/api/main.py::_detect_atomic_values (kept in sync).
-- Updates public (the seed source for NEW tenants — provisioning copies
-- public.extraction_patterns) AND every already-provisioned faultline_% schema.
--
-- Idempotent: the UPDATE is keyed on (category, rel_type); re-running is a no-op once the
-- regex already equals the target. No DROP, no destructive SQL, no data loss.
-- GUARD ADDED (gauntlet first-boot-migration-corpus round 2, 2026-09-16): `AND NOT EXISTS
-- (<the corrected row>)`, the same shape 198 uses. BEHAVIOUR-PRESERVING: where the original
-- statement succeeded (one has_phone row, not yet corrected) the guard is false and the UPDATE
-- runs exactly as before; where the original FAILED — the corrected row already present beside
-- the original 060 row that a broken 060 re-ran back into existence — the UPDATE used to try to
-- make two rows equal under UNIQUE (pattern_regex, rel_type) (SQLSTATE 23505) on every boot of
-- every pre-ledger box, and now touches nothing (276 removes the resurrected row). No state that
-- previously converged converges differently; the one that never converged now does, in one boot.

-- ── public seed template (NEW tenants) ──────────────────────────────────────
UPDATE public.extraction_patterns
   SET pattern_regex = $q$(?<!\d)(?:\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}|\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})\b$q$
 WHERE category = 'scalar_atomic' AND rel_type = 'has_phone'
   AND NOT EXISTS (
         SELECT 1 FROM public.extraction_patterns x
          WHERE x.category = 'scalar_atomic' AND x.rel_type = 'has_phone'
            AND x.pattern_regex = $q$(?<!\d)(?:\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}|\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})\b$q$);

-- ── every already-provisioned tenant schema ─────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($f$
            UPDATE %I.extraction_patterns
               SET pattern_regex = $q$(?<!\d)(?:\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}|\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})\b$q$
             WHERE category = 'scalar_atomic' AND rel_type = 'has_phone'
               AND NOT EXISTS (
                     SELECT 1 FROM %I.extraction_patterns x
                      WHERE x.category = 'scalar_atomic' AND x.rel_type = 'has_phone'
                        AND x.pattern_regex = $q$(?<!\d)(?:\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}|\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})\b$q$)
        $f$, _schema, _schema);
    END LOOP;
END $$;
