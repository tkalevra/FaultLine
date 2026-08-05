-- Migration 216: linguistic_cues — seed the identifier_noun CONTEXT-SIGNAL cue class
-- Date: 2026-08-04
--
-- WHY
-- ---
-- Migration 215 added a VALUE-SHAPE atomic pattern (has_reference_id) for alphanumeric identifier
-- CODES ("ABC-12345", "2024-CV-00931"), gated to require letters+digits so it never eats a plain
-- number (a bare count). But a user who DIRECTLY STATES a PURE-NUMERIC identifier WITH CONTEXT —
-- "my ticket number is 1234567", "my case number is 8891002", "the confirmation number is 55021" —
-- is still dropped by that gate (correctly: a bare number is shape-indistinguishable from a count).
--
-- The disambiguator is the CONTEXT NOUN: "1234567" after "ticket number is" is an IDENTIFIER, not a
-- count. This migration seeds that signal as a bounded, per-tenant, GROWABLE cue class on the SAME
-- linguistic_cues rail as kinship_noun / unit_scalar. The spine deriver's _chain_identifier_context
-- (src/extraction/linguistics.py) reads it: "my/the <NP with a STRONG identifier_noun cue> is
-- <digit-bearing value>" → (owner, has_reference_id, <verbatim value>), captured REGARDLESS of value
-- shape. NO in-code head-noun list — the vocabulary lives here and grows.
--
-- ROLE (the `description` column, resolved by resolve_identifier_noun_roles → {noun: role})
-- ----------------------------------------------------------------------------------------
--   • 'strong' — INHERENTLY identifier-signalling; establishes the context ALONE ("my case is X",
--     "my id is X", "my reference is X") or as a COMPOUND of a generic head ("ticket number").
--   • 'suffix' — the AMBIGUOUS generic tail. ONLY "number" is a suffix: a bare "number" is
--     "favorite number" / "phone number" / a count, so it NEVER triggers alone — the deriver gate
--     requires a 'strong' cue present in the NP. ("id"/"code" are 'strong': they mean identifier
--     unambiguously.)
--
-- PRECEDENCE (documented, deterministic):
--   • phone/ip/email/date NEVER reach this path: "phone" is NOT an identifier_noun cue, so
--     "my phone number is 519-555-0123" is untouched here and the has_phone value-shape atomic wins.
--   • "my favorite number is 7" / "my lucky number is 13" → no STRONG cue ("favorite"/"lucky" are
--     not cues, "number" is only a suffix) → this path does NOT fire; the value is not an identifier.
--   • An alphanumeric id in an identifier-context sentence ("my ticket number is ABC-12345") is
--     claimed by BOTH this chain and the mig-147 atomic as (user, has_reference_id, "ABC-12345") —
--     the harvest's (subject, rel, object) dedup collapses them to ONE edge.
--
-- has_reference_id is a SCALAR rel (tail_types={SCALAR}, migration 215) → routed to entity_attributes
-- exactly like has_ip / age; the value is stored VERBATIM (scalar_datatype='string').
--
-- Mirrors the DB-DOWN code-fallback in linguistic_cue_overlay._BOOTSTRAP_IDENTIFIER_NOUN_ROLE_MAP.
-- NO DDL change: migration 105 created public.linguistic_cues (+ per-tenant), and the provisioning
-- seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into every NEW
-- tenant — so new tenants inherit this class automatically. This migration only (1) seeds public and
-- (2) fans out to EXISTING tenant schemas. Idempotent: ON CONFLICT (cue, category) DO NOTHING.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL).
CREATE TABLE IF NOT EXISTS public.linguistic_cues (
    id                SERIAL PRIMARY KEY,
    cue               VARCHAR(128) NOT NULL,
    category          VARCHAR(64)  NOT NULL DEFAULT 'naming_verb',
    frequency         INT   DEFAULT 0,
    confirmed_count   INT   DEFAULT 0,
    rejected_count    INT   DEFAULT 0,
    correction_count  INT   DEFAULT 0,
    global_confidence FLOAT DEFAULT 0.5,
    description       TEXT,
    example_text      TEXT,
    source            VARCHAR(64),
    is_active         BOOLEAN DEFAULT true,
    archived_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW(),
    last_matched_at   TIMESTAMP,
    UNIQUE (cue, category)
);

-- ── Part 1: seed public (TEMPLATE / SEED-SOURCE ONLY) ────────────────────────
-- `cue` = the spaCy noun lemma (lowercase); `description` = the ROLE ('strong' | 'suffix').
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('ticket',       'identifier_noun', 'strong', 'my ticket number is 1234567',       'seed_identifier_noun', 0.92),
  ('case',         'identifier_noun', 'strong', 'my case number is 8891002',         'seed_identifier_noun', 0.92),
  ('docket',       'identifier_noun', 'strong', 'the docket number is 2024-CV-00931','seed_identifier_noun', 0.92),
  ('order',        'identifier_noun', 'strong', 'my order number is 55021',          'seed_identifier_noun', 0.90),
  ('account',      'identifier_noun', 'strong', 'my account number is 004417',       'seed_identifier_noun', 0.90),
  ('policy',       'identifier_noun', 'strong', 'my policy number is PN-99812',      'seed_identifier_noun', 0.90),
  ('claim',        'identifier_noun', 'strong', 'my claim number is 7788-AA',        'seed_identifier_noun', 0.88),
  ('reference',    'identifier_noun', 'strong', 'my reference is REF-2024-8891',     'seed_identifier_noun', 0.90),
  ('confirmation', 'identifier_noun', 'strong', 'the confirmation number is 55021',  'seed_identifier_noun', 0.90),
  ('invoice',      'identifier_noun', 'strong', 'the invoice number is INV-0012',    'seed_identifier_noun', 0.88),
  ('id',           'identifier_noun', 'strong', 'my employee id is 4471',            'seed_identifier_noun', 0.88),
  ('code',         'identifier_noun', 'strong', 'my code is 4471X',                  'seed_identifier_noun', 0.85),
  ('number',       'identifier_noun', 'suffix', 'my ticket number is 1234567',       'seed_identifier_noun', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

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
            INSERT INTO %I.linguistic_cues
                (cue, category, description, example_text, source, global_confidence)
            VALUES
              ('ticket',       'identifier_noun', 'strong', 'my ticket number is 1234567',       'seed_identifier_noun', 0.92),
              ('case',         'identifier_noun', 'strong', 'my case number is 8891002',         'seed_identifier_noun', 0.92),
              ('docket',       'identifier_noun', 'strong', 'the docket number is 2024-CV-00931','seed_identifier_noun', 0.92),
              ('order',        'identifier_noun', 'strong', 'my order number is 55021',          'seed_identifier_noun', 0.90),
              ('account',      'identifier_noun', 'strong', 'my account number is 004417',       'seed_identifier_noun', 0.90),
              ('policy',       'identifier_noun', 'strong', 'my policy number is PN-99812',      'seed_identifier_noun', 0.90),
              ('claim',        'identifier_noun', 'strong', 'my claim number is 7788-AA',        'seed_identifier_noun', 0.88),
              ('reference',    'identifier_noun', 'strong', 'my reference is REF-2024-8891',     'seed_identifier_noun', 0.90),
              ('confirmation', 'identifier_noun', 'strong', 'the confirmation number is 55021',  'seed_identifier_noun', 0.90),
              ('invoice',      'identifier_noun', 'strong', 'the invoice number is INV-0012',    'seed_identifier_noun', 0.88),
              ('id',           'identifier_noun', 'strong', 'my employee id is 4471',            'seed_identifier_noun', 0.88),
              ('code',         'identifier_noun', 'strong', 'my code is 4471X',                  'seed_identifier_noun', 0.85),
              ('number',       'identifier_noun', 'suffix', 'my ticket number is 1234567',       'seed_identifier_noun', 0.80)
            ON CONFLICT (cue, category) DO NOTHING
        $f$, _schema);
    END LOOP;
END $$;
