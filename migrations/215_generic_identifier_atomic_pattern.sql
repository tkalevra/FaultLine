-- Migration 215: generic reference/identifier CODE as a scalar_atomic pattern
-- Date: 2026-08-04
--
-- WHY
-- ---
-- The seeded scalar_atomic inventory (migration 060 + 184) protects IP/CIDR/MAC/email/
-- phone/date/FQDN/URL/UUID/port — the structured literals the LLM/deriver would MANGLE on
-- their internal separators. It has NO pattern for a GENERIC alphanumeric identifier
-- (ticket / case / docket / reference / order / invoice numbers: "ABC-12345",
-- "2024-CV-00931", "7788-AA", "INC0012345", "REF-2024-8891"). A cleanly-STATED identifier
-- ("My case number for the Capital One claim is 7788-AA") therefore never gets atomic
-- protection: the value falls to the LLM/spine deriver, which splits it on its dashes
-- ("CVE-2024-9999" → "CVE-2024" / "9999") — the same class of shredding that gave
-- "your phone number is 123" before migration 184. The identifier is dropped or mangled.
--
-- FIX (subject-agnostic, metadata-driven, deterministic — NO domain literals in Python)
-- ------------------------------------------------------------------------------------
-- Add ONE scalar_atomic FORMAT-GRAMMAR row that types the identifier SHAPE and routes it,
-- via the SAME machinery IP/email use (`_detect_atomic_values` → possessive-attribute
-- connect / residue subject-binder → `has_reference_id` SCALAR on the owning entity), so
-- the value is captured whole and reachable on the dumb walk. `has_reference_id` is a
-- SCALAR rel (tail_types = {SCALAR}, Class B) — routed to entity_attributes exactly like
-- has_ip / has_email, never resolved to a UUID.
--
-- THE DETERMINISTIC GATE (why it does NOT eat plain numbers or words)
-- ------------------------------------------------------------------
-- `_detect_atomic_values` runs finditer WITHOUT re.IGNORECASE, so this pattern is
-- CASE-SENSITIVE by design. It matches a token of [0-9A-Z] joined by [#/-] separators that
-- carries BOTH:
--     >= 1 UPPERCASE letter   AND   >= 2 digits
--   • no letter  → plain number  → NOT matched  ("45", "4471", "2024", "00931")
--   • no digit / no uppercase → plain or lowercase word → NOT matched
--     ("ticket", "docket", "Capital", "utf8", "win10", "iPhone13", "mp3", "PS5", "B2B")
-- Boundary guards `(?<![\w.:@#/-]) … (?![\w])(?![.:@]\w)` reject adjacency to . : / @ so it
-- never carves a fragment out of an IP / IPv6 / email / FQDN / URL span; a MAC-shape
-- negative lookahead yields an uppercase hyphen-MAC ("AA-BB-CC-DD-EE-01") to has_mac.
-- (Those more-specific patterns also claim their span first via the longer-match-wins
-- dedup — the guards make correctness independent of pattern order.)
-- HONEST residual: a PURE-NUMERIC id ("invoice 4471") is intentionally NOT captured — it is
-- grammatically indistinguishable from a count, and eating every bare number is the exact
-- failure this gate exists to avoid. Uppercase model/version tokens ("RTX3080", "COVID19")
-- are identifier-shaped collateral: Class-B, user-correctable, re_embedder-tunable.
--
-- Mirrors the _BOOTSTRAP fallback in src/api/main.py::_detect_atomic_values (kept in sync,
-- per migration 184's convention). Updates public (the seed source — provisioning copies
-- public.extraction_patterns / public.rel_types into each NEW tenant) AND every already-
-- provisioned faultline_% schema. Idempotent: ON CONFLICT DO NOTHING on both tables; no
-- DROP, no destructive SQL, no data loss.

-- ── rel_type: has_reference_id (SCALAR, Class B) ─────────────────────────────
-- source must satisfy rel_types_source_check (wikidata|builtin|engine|user|expand);
-- scalar_datatype='string' resolves the value deterministically to the untyped-string
-- storage path (no strict format validator — an identifier has no canonical shape).
INSERT INTO public.rel_types
    (rel_type, label, head_types, tail_types, is_symmetric, is_hierarchy_rel,
     category, fact_class, scalar_datatype, engine_generated, source, confidence)
VALUES
    ('has_reference_id', 'Has reference identifier', ARRAY['ANY'], ARRAY['SCALAR'],
     false, false, 'identity', 'B', 'string', true, 'builtin', 0.85)
ON CONFLICT (rel_type) DO NOTHING;

-- ── scalar_atomic extraction pattern (public seed → NEW tenants) ─────────────
INSERT INTO public.extraction_patterns
    (pattern_regex, rel_type, description, example_text, category, source, global_confidence)
VALUES
    ($q$(?<![\w.:@#/-])(?!(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-]))(?=[0-9A-Z#/-]*[A-Z])(?=(?:[0-9A-Z#/-]*[0-9]){2})[0-9A-Z]+(?:[#/-][0-9A-Z]+)*(?![\w])(?![.:@]\w)$q$,
     'has_reference_id',
     'Generic reference/identifier code (letters+digits+separators): ticket/case/docket/order/invoice number',
     'ABC-12345',
     'scalar_atomic', 'bootstrap', 0.72)
ON CONFLICT (pattern_regex, rel_type) DO NOTHING;

-- ── backfill every already-provisioned tenant schema ─────────────────────────
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
            INSERT INTO %I.rel_types
                (rel_type, label, head_types, tail_types, is_symmetric, is_hierarchy_rel,
                 category, fact_class, scalar_datatype, engine_generated, source, confidence)
            VALUES
                ('has_reference_id', 'Has reference identifier', ARRAY['ANY'], ARRAY['SCALAR'],
                 false, false, 'identity', 'B', 'string', true, 'builtin', 0.85)
            ON CONFLICT (rel_type) DO NOTHING
        $f$, _schema);

        EXECUTE format($f$
            INSERT INTO %I.extraction_patterns
                (pattern_regex, rel_type, description, example_text, category, source, global_confidence)
            VALUES
                ($q$(?<![\w.:@#/-])(?!(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-]))(?=[0-9A-Z#/-]*[A-Z])(?=(?:[0-9A-Z#/-]*[0-9]){2})[0-9A-Z]+(?:[#/-][0-9A-Z]+)*(?![\w])(?![.:@]\w)$q$,
                 'has_reference_id',
                 'Generic reference/identifier code (letters+digits+separators): ticket/case/docket/order/invoice number',
                 'ABC-12345',
                 'scalar_atomic', 'bootstrap', 0.72)
            ON CONFLICT (pattern_regex, rel_type) DO NOTHING
        $f$, _schema);
    END LOOP;
END $$;
