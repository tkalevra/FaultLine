-- Migration 061: Fix networking rel_types — Class B + SCALAR tail_types
-- Date: 2026-06-01
--
-- Problem: Networking rel_types were seeded as fact_class='C' (ephemeral, Qdrant-only,
-- expires 30 days). IP address bindings, MAC addresses, emails, hostnames are behavioral
-- facts — they should be fact_class='B' (staged, immediately visible via PostgreSQL UNION,
-- promoted after 3 confirmations).
--
-- Additionally, tail_types was NULL for these rel_types. It must be ARRAY['SCALAR'] so the
-- classification engine routes to entity_attributes (string storage) rather than the facts
-- table (UUID entity storage). IPs, MACs, emails are STRING values, not UUID entity refs.
--
-- NOTE: The startup seed in main.py uses ON CONFLICT DO NOTHING which prevents future
-- corrections like this one from taking effect on rows seeded at startup. That seeding
-- should be changed to ON CONFLICT DO UPDATE in code — but that is a code change, not
-- a migration. Document this here for the next code review cycle.

-- ── Step 1: Correct existing rows (already in public.rel_types) ───────────────────────
-- GREATEST() preserves any higher confidence value that may have been set by the LLM
-- inference path; never downgrade confidence unilaterally.

UPDATE public.rel_types
SET
    fact_class = 'B',
    tail_types = ARRAY['SCALAR'],
    confidence = GREATEST(confidence, 0.6)
WHERE rel_type IN (
    'has_ip', 'hostname', 'has_hostname', 'fqdn', 'has_fqdn',
    'has_mac', 'has_url', 'has_email', 'has_phone',
    'has_subnet', 'has_uuid', 'located_at'
);

-- ── Step 2: Insert any rows missing from public.rel_types ────────────────────────────
-- ON CONFLICT DO UPDATE ensures this migration (and future migrations) can correct
-- stale values set by earlier ON CONFLICT DO NOTHING seeds. This is the correct
-- pattern per the growth architecture: rel_types table IS the ontology; migrations
-- are the mechanism for evolving it.

INSERT INTO public.rel_types (
    rel_type, label, fact_class, tail_types, source, category,
    confidence, is_symmetric, is_hierarchy_rel, is_leaf_only, allows_leaf_rels
)
VALUES
    ('has_ip',       'Has IP Address',    'B', ARRAY['SCALAR'], 'builtin', 'network',   0.8,  false, false, true, NULL),
    ('hostname',     'Hostname',          'B', ARRAY['SCALAR'], 'builtin', 'network',   0.7,  false, false, true, NULL),
    ('has_hostname', 'Has Hostname',      'B', ARRAY['SCALAR'], 'builtin', 'network',   0.7,  false, false, true, NULL),
    ('fqdn',         'FQDN',             'B', ARRAY['SCALAR'], 'builtin', 'network',   0.7,  false, false, true, NULL),
    ('has_fqdn',     'Has FQDN',         'B', ARRAY['SCALAR'], 'builtin', 'network',   0.7,  false, false, true, NULL),
    ('has_mac',      'Has MAC Address',  'B', ARRAY['SCALAR'], 'builtin', 'network',   0.8,  false, false, true, NULL),
    ('has_url',      'Has URL',          'B', ARRAY['SCALAR'], 'builtin', 'network',   0.7,  false, false, true, NULL),
    ('has_email',    'Has Email',        'B', ARRAY['SCALAR'], 'builtin', 'network',   0.8,  false, false, true, NULL),
    ('has_phone',    'Has Phone',        'B', ARRAY['SCALAR'], 'builtin', 'network',   0.7,  false, false, true, NULL),
    ('has_subnet',   'Has Subnet',       'B', ARRAY['SCALAR'], 'builtin', 'network',   0.7,  false, false, true, NULL),
    ('has_uuid',     'Has UUID',         'B', ARRAY['SCALAR'], 'builtin', 'network',   0.6,  false, false, true, NULL),
    ('located_at',   'Located At',       'B', ARRAY['SCALAR'], 'builtin', 'location',  0.7,  false, false, true, NULL)
ON CONFLICT (rel_type) DO UPDATE SET
    fact_class = EXCLUDED.fact_class,
    tail_types = EXCLUDED.tail_types,
    confidence = GREATEST(public.rel_types.confidence, EXCLUDED.confidence),
    category   = EXCLUDED.category;
