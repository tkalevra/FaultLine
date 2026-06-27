-- Migration 099: subject-agnostic naming relation ("X named/called Y")
-- Date: 2026-06-19
-- Purpose (RC2): "I have a dog named Rex." was dropping the name. TWO defects fixed here:
--
--   (1) RUNG-1 morphology over-stems the naming verb: normalize_rel("named") = "nam" (the
--       conservative -ed strip), and "nam" matches NO rel_type and NO alias → the verb-lift
--       path in main.py minted a junk grown rel (dog, nam, Rex). The honest-dumb-stemmer
--       contract (canonical.py) deliberately leaves over-stems to be BRIDGED by the DB alias
--       layer (same mechanism that makes "called" → normalize "call" → alias → also_known_as,
--       seeded in migration 030). "named" was simply MISSING from that seed. We add the
--       naming-verb aliases so resolve_canonical() bridges the over-stem to also_known_as /
--       pref_name instead of minting junk. No code/morphology change required.
--
--   (2) There was no SUBJECT-AGNOSTIC extraction rule for "<noun> (named|called) <ProperName>"
--       that binds the name to the PRECEDING head-noun (the dog), not to "user". The only
--       naming patterns were the first-person self-identity ones ("my name is X" → user). We
--       add a deterministic TWO-GROUP extraction_patterns rule whose group[0] is the head noun
--       and group[1] is the proper name; compound.py's existing two-group also_known_as path
--       emits (head-noun, also_known_as, ProperName) — subject = the captured noun, never "user".
--
-- HARD CONSTRAINTS honored: deterministic (regex + DB alias, NO cosine/LLM); metadata-driven
-- (no rel_type literals in code; the rel mapping lives in DB rows); subject-agnostic (the regex
-- captures whatever common noun precedes the naming verb — no hardcoded subject/pronoun list);
-- per-tenant (seed public TEMPLATE, then fan out to every faultline_% schema; runtime reads the
-- tenant copy on the no-public search_path); fail-safe + idempotent (ON CONFLICT DO NOTHING).
--
-- public is the SEED SOURCE / template only. Future tenants inherit both seeds via
-- schema_manager bootstrap (copies public.rel_type_aliases + public.extraction_patterns).

-- ============================================================================
-- PART (a): naming-verb ALIASES — bridge the RUNG-1 over-stem to the canonical
--           naming relations so the verb-lift resolves instead of minting "nam".
-- ============================================================================
-- The alias loader (canonical._load_aliases) indexes each row BOTH raw and under
-- normalize_rel(alias). So alias='named' resolves a surface "named" (which RUNG-1 normalizes
-- to "nam"); alias='name' resolves a surface "name". Mirrors the existing 'called' → also_known_as
-- seed (migration 030). also_known_as = an alias-naming relation (skos:altLabel); pref_name =
-- the preferred-name naming relation (skos:prefLabel). Both are LIVE canonical rel_types.
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source) VALUES
    ('also_known_as', 'named', 'ontology'),
    ('pref_name',     'name',  'ontology')
ON CONFLICT (alias) DO NOTHING;

-- ============================================================================
-- PART (b): subject-agnostic naming CONSTRUCTION — "<noun> (named|called) <ProperName>"
-- ============================================================================
-- TWO capture groups → compound.py also_known_as two-group path → (group0, also_known_as, group1).
--   group 1: the preceding common noun (the entity being named: dog, cat, server, boat, …).
--            [a-z]+ keeps it a single lowercased common noun (proper nouns are the NAME, not the
--            thing being named) — subject-agnostic, no enumerated subject list.
--   group 2: the proper name ([A-Z][a-z]+).
-- The leading (?:a|an|the|my|our|your|his|her|their)? optionally consumes a determiner/possessive
-- so it is NOT captured as the noun ("a dog named Rex" → noun='dog', not 'a'). This is bounded
-- English determiner morphology, not an ontology/subject list.
INSERT INTO public.extraction_patterns
    (pattern_regex, rel_type, description, example_text, category, source, global_confidence)
VALUES
  ('\b(?:a|an|the|my|our|your|his|her|their)?\s*([a-z]+)\s+(?:named|called)\s+([a-z]+)',
   'also_known_as',
   'Subject-agnostic naming: <noun> named/called <ProperName> → (noun, also_known_as, Name)',
   'I have a dog named Rex',
   'identity', 'bootstrap', 0.85)
ON CONFLICT (pattern_regex, rel_type) DO NOTHING;

-- ============================================================================
-- PART (c): fan out BOTH seeds to every existing faultline_% tenant
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- rel_type_aliases: only copy rows whose canonical_rel_type is a LIVE rel_type in THIS
        -- tenant (the FK would reject others; the WHERE guard keeps the copy clean).
        EXECUTE format($seed_alias$
            INSERT INTO %I.rel_type_aliases
                (canonical_rel_type, alias, source, confidence,
                 requires_inversion, is_symmetric, inverse_alias)
            SELECT a.canonical_rel_type, a.alias, a.source, a.confidence,
                   a.requires_inversion, a.is_symmetric, a.inverse_alias
            FROM public.rel_type_aliases a
            WHERE a.alias IN ('named', 'name')
              AND a.canonical_rel_type IN (SELECT rel_type FROM %I.rel_types)
            ON CONFLICT (alias) DO NOTHING
        $seed_alias$, _schema, _schema);

        -- extraction_patterns: copy the naming-construction row.
        EXECUTE format($seed_pat$
            INSERT INTO %I.extraction_patterns
                (pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 category, source, is_active, archived_at, last_matched_at)
            SELECT pattern_regex, rel_type, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   category, source, is_active, archived_at, last_matched_at
            FROM public.extraction_patterns
            WHERE category = 'identity'
              AND rel_type = 'also_known_as'
              AND pattern_regex = '\b(?:a|an|the|my|our|your|his|her|their)?\s*([a-z]+)\s+(?:named|called)\s+([a-z]+)'
            ON CONFLICT (pattern_regex, rel_type) DO NOTHING
        $seed_pat$, _schema);

        RAISE NOTICE 'Migration 099: seeded naming alias + construction into %', _schema;
    END LOOP;
END $$;
