-- Migration 198: extraction_patterns — a CLOCK TIME is not a network PORT (and fix the port VALUE)
-- Date: 2026-07-31
--
-- THE DEFECT (reproduced, deterministic — pure regex, no model involved)
-- ----------------------------------------------------------------------
-- The seeded scalar_atomic pattern for `has_port` (migration 060) is:
--
--     (?:port\s+|:)([1-9]\d{0,4})\b
--
-- Its second alternative matches a BARE COLON followed by digits, ANYWHERE. So the session-date
-- marker every ingested turn carries —
--
--     [Date: 2023/05/22 (Mon) 22:17]
--
-- matches `:17`, and the deterministic atomic-scalar layer proposes a `has_port` scalar. The
-- subject binder then attaches it to whatever entity is in scope, so an arbitrary entity acquires
-- a fabricated network attribute ON EVERY TURN. Measured against the live regex:
--
--     '[Date: 2023/05/22 (Mon) 22:17]'   -> ':17'      (clock time)
--     'My meeting is at 14:30.'          -> ':30'      (clock time)
--     'I woke at 6:45 and left at 7:20.' -> ':45' ':20' (clock times)
--     'The score was 3:1.'               -> ':1'       (ratio)
--
-- A colon standing between two digit groups is a time-of-day separator, not a port delimiter. The
-- primary source is unambiguous about what makes a colon a PORT marker — RFC 3986 §3.2.3 ("Port"):
-- "The port subcomponent of authority is designated by an optional port number in decimal FOLLOWING
-- THE HOST and delimited from it by a single colon (':') character." The colon is a port marker
-- solely by virtue of what stands to its LEFT. The seeded pattern asserted that marker with NO left
-- context at all, which is exactly why it cannot tell 22:17 from example.com:8443.
--
-- THE SECOND, INDEPENDENT DEFECT (found while fixing the first — the GENUINE lane never worked)
-- ---------------------------------------------------------------------------------------------
-- `_detect_atomic_values` (src/api/main.py) takes the value from `m.group(0)` — the WHOLE match —
-- and DISCARDS the capture group. So even a correct hit stored its own marker:
--
--     'The server listens on port 8080.'  -> value 'port 8080'
--     'Connect to 192.168.1.50:8080'      -> value ':8080'
--
-- Migration 101 set `rel_types.has_port.scalar_datatype = 'integer'`, and BOTH ingest gates fall
-- back to that when the detector label maps to no datatype (the label here is the pattern's
-- description, 'Network port number', which is not in `_DETECTOR_TYPE_TO_DATATYPE`). Validation
-- routes 'integer' through `_parse_numeric_scalar`, which anchors on a LEADING numeric token —
-- 'port 8080' and ':8080' both fail it, so the edge is rejected. CONSEQUENCE: there is currently
-- NO input that yields a stored `has_port` value. The lane produces only noise and rejections.
-- Tightening it therefore cannot lose a capture that works today; it makes the lane work at all.
--
-- THE REPLACEMENT
-- ---------------
--   (?:(?<=[Pp]ort )|(?<=[Pp]orts )|(?<=[Pp]ort:)|(?<=PORT )|(?<=PORT:)
--    |(?<=\.[A-Za-z]:)|(?<=\.[A-Za-z]{2}:)|(?<=\.[A-Za-z]{3}:)|(?<=\.[A-Za-z]{4}:)
--    |(?<=\.\d:)|(?<=\.\d\d:)|(?<=\.\d\d\d:))
--   (?:6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3})(?!\d)(?!\.\d)
--
-- Three properties, each deliberate:
--  1. The marker sits in a LOOKBEHIND, so `m.group(0)` is the BARE NUMBER — a valid `integer`
--     scalar that passes the migration-101 datatype gate. (Python `re` requires fixed-width
--     lookbehinds; the alternation of several fixed-width lookbehinds is how the variable-width
--     left contexts are expressed. Verified to compile.)
--  2. The bare-colon lane is replaced by a HOST-QUALIFIED colon: the colon must follow a DOTTED
--     token — `.<letters>:` (example.com:8443, db.internal.lan:5432) or `.<digits>:` (the final
--     octet of a dotted quad, 192.168.1.50:8080). This is RFC 3986's "colon following the host"
--     expressed structurally. A clock time NEVER has a dot immediately before the colon, so
--     22:17 / 14:30 / 6:45 / 3:1 are excluded by CONSTRUCTION, not by a value blacklist.
--  3. The numeric alternation bounds the value to 1-65535. RFC 6335 §6 ("Port Number Ranges"):
--     "TCP, UDP, UDP-Lite, SCTP, and DCCP use 16-bit namespaces for their port number registries",
--     with the Dynamic/Private range ending at 65535 — so 65535 is the ceiling and 'port 70000' no
--     longer mints a scalar. The trailing (?!\d)(?!\.\d) keeps it off longer numbers and off the
--     interior of a dotted quad / version string, while still allowing a SENTENCE-FINAL port
--     ("...on port 8080." — an earlier draft used (?![\d.]) and silently dropped exactly that,
--     caught by the test below, not by inspection).
--
-- MEASURED before -> after (OLD group(0) -> NEW group(0)):
--     '[Date: 2023/05/22 (Mon) 22:17]'   ':17'        -> (none)     FIXED
--     'My meeting is at 14:30.'          ':30'        -> (none)     FIXED
--     'I woke at 6:45 and left at 7:20.' ':45',':20'  -> (none)     FIXED
--     'The score was 3:1.'               ':1'         -> (none)     FIXED
--     'port 70000 is invalid'            'port 70000' -> (none)     FIXED (out of range)
--     'The server listens on port 8080.' 'port 8080'  -> '8080'     now a VALID integer scalar
--     'Set Port 443 on the firewall.'    (none)       -> '443'      newly captured (was case-blind)
--     'Open ports 80 and 443.'           (none)       -> '80'       newly captured
--     'Use port:22 for ssh.'             ':22'        -> '22'       now a VALID integer scalar
--     'Connect to 192.168.1.50:8080'     ':8080'      -> '8080'     now a VALID integer scalar
--     'https://example.com:8443/api'     ':8443'      -> '8443'     now a VALID integer scalar
--     'db.internal.lan:5432 is the DSN'  ':5432'      -> '5432'     now a VALID integer scalar
--     'My IP is 192.168.1.50'            (none)       -> (none)     unchanged
--     'MAC is 00:1a:2b:3c:4d:5e'         (none)       -> (none)     unchanged
--
-- AUTHORITY ORDER (user > seed > growth) — this is a SEED correction, explicitly scoped.
-- The UPDATE is restricted to `source = 'bootstrap'` and to the EXACT migration-060 regex, so it
-- can only ever touch the row this project seeded. A tenant-GROWN or user-corrected `has_port`
-- pattern carries a different `pattern_regex` (the table's uniqueness key) and/or a different
-- `source`, and is left untouched — growth and user corrections still sit ABOVE the seed. The
-- correction is STRUCTURAL (left-context grammar + the RFC 6335 port range), never a list of
-- values or names to exclude.
--
-- NO TEMPLATE EDIT REQUIRED — and this was verified, not assumed:
-- src/provisioning/templates/user_schema.sql only CREATEs extraction_patterns (no INSERT of rows
-- anywhere in the file); the rows are copied from public.extraction_patterns at provisioning by
-- src/provisioning/schema_manager.py. So fixing public covers every NEW tenant automatically, and
-- Part 2 below fans the fix out to already-provisioned tenants (which the provisioning copy, being
-- ON CONFLICT DO NOTHING, would never reach).
--
-- NO DDL CHANGE. Idempotent: re-running matches nothing the second time. Safe to re-run.
-- NOTE: after applying, FLUSH the pattern cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the TTL.
--
-- REVERT (should this ever need backing out) — restore the migration-060 row:
--   UPDATE public.extraction_patterns
--      SET pattern_regex = '(?:port\s+|:)([1-9]\d{0,4})\b'
--    WHERE rel_type = 'has_port' AND source = 'bootstrap';
--   (and the same statement per faultline_% schema)

-- ── Part 1: correct the seed (TEMPLATE / SEED-SOURCE ONLY) ───────────────────
UPDATE public.extraction_patterns
   SET pattern_regex = '(?:(?<=[Pp]ort )|(?<=[Pp]orts )|(?<=[Pp]ort:)|(?<=PORT )|(?<=PORT:)|(?<=\.[A-Za-z]:)|(?<=\.[A-Za-z]{2}:)|(?<=\.[A-Za-z]{3}:)|(?<=\.[A-Za-z]{4}:)|(?<=\.\d:)|(?<=\.\d\d:)|(?<=\.\d\d\d:))(?:6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3})(?!\d)(?!\.\d)',
       description   = 'Network port number (host-qualified colon or explicit port marker; RFC 6335 range 1-65535)',
       example_text  = 'port 8080',
       updated_at    = NOW()
 WHERE rel_type      = 'has_port'
   AND source        = 'bootstrap'
   AND pattern_regex = '(?:port\s+|:)([1-9]\d{0,4})\b'
   AND NOT EXISTS (
         SELECT 1 FROM public.extraction_patterns x
          WHERE x.rel_type = 'has_port'
            AND x.pattern_regex = '(?:(?<=[Pp]ort )|(?<=[Pp]orts )|(?<=[Pp]ort:)|(?<=PORT )|(?<=PORT:)|(?<=\.[A-Za-z]:)|(?<=\.[A-Za-z]{2}:)|(?<=\.[A-Za-z]{3}:)|(?<=\.[A-Za-z]{4}:)|(?<=\.\d:)|(?<=\.\d\d:)|(?<=\.\d\d\d:))(?:6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3})(?!\d)(?!\.\d)');

-- ── Part 2: fan out to every already-provisioned tenant schema ───────────────
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
               SET pattern_regex = '(?:(?<=[Pp]ort )|(?<=[Pp]orts )|(?<=[Pp]ort:)|(?<=PORT )|(?<=PORT:)|(?<=\.[A-Za-z]:)|(?<=\.[A-Za-z]{2}:)|(?<=\.[A-Za-z]{3}:)|(?<=\.[A-Za-z]{4}:)|(?<=\.\d:)|(?<=\.\d\d:)|(?<=\.\d\d\d:))(?:6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3})(?!\d)(?!\.\d)',
                   description   = 'Network port number (host-qualified colon or explicit port marker; RFC 6335 range 1-65535)',
                   example_text  = 'port 8080',
                   updated_at    = NOW()
             WHERE rel_type      = 'has_port'
               AND source        = 'bootstrap'
               AND pattern_regex = '(?:port\s+|:)([1-9]\d{0,4})\b'
               AND NOT EXISTS (
                     SELECT 1 FROM %I.extraction_patterns x
                      WHERE x.rel_type = 'has_port'
                        AND x.pattern_regex = '(?:(?<=[Pp]ort )|(?<=[Pp]orts )|(?<=[Pp]ort:)|(?<=PORT )|(?<=PORT:)|(?<=\.[A-Za-z]:)|(?<=\.[A-Za-z]{2}:)|(?<=\.[A-Za-z]{3}:)|(?<=\.[A-Za-z]{4}:)|(?<=\.\d:)|(?<=\.\d\d:)|(?<=\.\d\d\d:))(?:6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|[1-9]\d{0,3})(?!\d)(?!\.\d)')
        $f$, _schema, _schema);
    END LOOP;
END $$;
