-- Migration 060: Atomic scalar value patterns for pre-flight extraction protection
--
-- These patterns are matched against input text BEFORE the LLM extraction call.
-- Detected values are injected as type annotations into the extraction prompt,
-- preventing the LLM from splitting structured values on their delimiters
-- (e.g., IP "192.0.2.20" split to "192" on the first octet).
--
-- category = 'scalar_atomic' marks them as pre-flight detectors, not
-- compound extraction patterns like category='identity' etc.
--
-- Ordered by specificity: more specific patterns have higher id so they
-- run first when ordered by LENGTH(pattern_regex) DESC.
-- Re-embedder Job 6 evaluates these and updates global_confidence over time.

INSERT INTO extraction_patterns
    (pattern_regex, rel_type, description, example_text, category, source, global_confidence)
VALUES

-- ── Network addresses (most specific → least specific) ──────────────────────

-- IPv4 CIDR (check before bare IPv4 — longer match wins on overlap)
(
  '\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{0,3}/\d{1,2}\b',
  'has_subnet',
  'IPv4 CIDR network range',
  '192.168.1.0/24',
  'scalar_atomic', 'bootstrap', 0.96
),

-- IPv4 address
(
  '\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b',
  'has_ip',
  'IPv4 address',
  '192.0.2.20',
  'scalar_atomic', 'bootstrap', 0.97
),

-- IPv6 full form
(
  '\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b',
  'has_ip',
  'IPv6 address full form',
  '2001:0db8:85a3:0000:0000:8a2e:0370:7334',
  'scalar_atomic', 'bootstrap', 0.95
),

-- IPv6 compressed
(
  '\b(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}\b',
  'has_ip',
  'IPv6 address compressed',
  '2001:db8::1',
  'scalar_atomic', 'bootstrap', 0.93
),

-- MAC address (colon or hyphen separated)
(
  '\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b',
  'has_mac',
  'MAC address colon-separated',
  'aa:bb:cc:dd:ee:ff',
  'scalar_atomic', 'bootstrap', 0.96
),
(
  '\b[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5}\b',
  'has_mac',
  'MAC address hyphen-separated',
  'aa-bb-cc-dd-ee-ff',
  'scalar_atomic', 'bootstrap', 0.95
),

-- ── Contact / identity ────────────────────────────────────────────────────────

-- Email address
(
  '\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b',
  'has_email',
  'Email address',
  'user@example.com',
  'scalar_atomic', 'bootstrap', 0.97
),

-- Phone E.164 international
(
  '\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}\b',
  'has_phone',
  'Phone number E.164 international format',
  '+1 (519) 555-0123',
  'scalar_atomic', 'bootstrap', 0.93
),

-- ── Dates ────────────────────────────────────────────────────────────────────

-- ISO 8601 date
(
  '\b\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b',
  'born_on',
  'ISO 8601 date YYYY-MM-DD',
  '1990-05-15',
  'scalar_atomic', 'bootstrap', 0.94
),

-- Human date: "15 May 1990", "May 15 1990", "May 15, 1990"
(
  '\b(?:\d{1,2}\s+)?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b',
  'born_on',
  'Human-readable date with month name',
  'May 15, 1990',
  'scalar_atomic', 'bootstrap', 0.90
),

-- ── Network identifiers ───────────────────────────────────────────────────────

-- Fully-qualified domain name (multiple labels, 2+ chars TLD)
(
  '\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.){2,}[a-zA-Z]{2,}\b',
  'has_fqdn',
  'Fully-qualified domain name',
  'your-server-host.example.com',
  'scalar_atomic', 'bootstrap', 0.91
),

-- TCP/UDP port (standalone, e.g., "port 8080" or ":8080")
(
  '(?:port\s+|:)([1-9]\d{0,4})\b',
  'has_port',
  'Network port number',
  'port 8080',
  'scalar_atomic', 'bootstrap', 0.88
),

-- ── Uniform identifiers ───────────────────────────────────────────────────────

-- URL (http/https, check before FQDN — URL is more specific)
(
  'https?://[^\s\)\"'']+',
  'has_url',
  'HTTP/HTTPS URL',
  'https://your-server-host.example.com/api',
  'scalar_atomic', 'bootstrap', 0.95
),

-- UUID v4
(
  '\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b',
  'has_uuid',
  'UUID v4 identifier',
  '550e8400-e29b-41d4-a716-446655440000',
  'scalar_atomic', 'bootstrap', 0.96
)

ON CONFLICT (pattern_regex, rel_type) DO NOTHING;

-- THE `INSERT INTO rel_types (...) VALUES (... 'bootstrap' ...)` THAT USED TO FOLLOW IS GONE
-- (gauntlet first-boot-migration-corpus, 2026-09-16). It seeded has_subnet / has_mac / has_email
-- / has_phone / has_port / has_url / has_uuid with source = 'bootstrap' — a value
-- `rel_types_source_check` (007: wikidata|builtin|engine|user; 072/073 add expand) has NEVER
-- admitted. PostgreSQL rejected the statement (SQLSTATE 23514 check_violation) on EVERY boot of
-- EVERY box, so the ledger recorded this file `failed` forever and re-ran it on every start —
-- and each re-run re-INSERTed the seed rows above, RESURRECTING the has_phone (migration 184)
-- and has_port (migration 198) regexes those migrations had corrected, as live duplicates
-- beside the corrected rows (measured: two active has_port rows, the clock-time false positive
-- 198 fixed among them). Six of the seven rels are seeded properly (source = 'builtin',
-- tail_types = {SCALAR}, fact_class = 'B') by 061_fix_networking_rel_types.sql; `has_port`, the
-- one 061 does not carry, is seeded by 275_seed_has_port_rel_type.sql with the metadata its
-- siblings received (061 + 101). The duplicate pattern rows are retired by
-- 276_retire_resurrected_060_pattern_rows.sql. With the dead statement gone this file applies
-- on the first pass and the ledger stops re-running it.
