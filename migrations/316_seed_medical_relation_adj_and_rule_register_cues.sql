-- Migration 316: linguistic_cues — seed the MEDICAL-RELATION ADJECTIVE + RULE-REGISTER CUE
-- classes (BUG-FAMILY-7, the sparse-store recall litmus, 2026-11-12).
--
-- WHY
-- ---
-- The honest sparse-store litmus FAILED 3/6 on a fresh 6-fact seat (DEV/BUGS/
-- family-smoke-20261111/BUG-FAMILY-7-sparse-store-litmus.md). Two of the three misses are
-- CLASS-shaped, not sentence-shaped, so the fix lands as SEEDED GROWABLE VOCABULARY (the
-- migration-308 discipline) + the code that consumes it:
--
--   1. medical_relation_adj — "I'm allergic to penicillin" minted the NOVEL rel
--      ``allergic_to`` (analyze_copula_relational_predicate's <adjective>_<preposition>
--      rule): the row LANDED and went DARK — the walk's aspect admission has no
--      ``allergy``→``allergic_to`` binding (the SEC-BUG-2 serve-side gap). The fix maps the
--      class member in the ``<adj> TO <NP>`` frame onto the DECLARED seeded shelf
--      ``has_allergy`` (migration 025; 314 stamps it is_sensitive=true) — the walk's
--      RECALL_HEALTH_SHELF admission reads the SAME surface class and serves the row on the
--      direct ask. The class is DB-HELD + per-tenant + GROWABLE: a non-member adjective in
--      the same frame records a cue candidate on the growth rail (freq≥3) so a tenant grows
--      its own members ("intolerant", "sensitive") without code edits. The seed is the ONE
--      measured member; the code fallback floor carries the same.
--
--   2. rule_register_cue — the snapshot-rule miss ("House rule for the lab: always snapshot
--      before a Proxmox upgrade.") had its cue present but no register frame captured the
--      value, and the degraded LLM lane dropped the turn whole. The register arm's cue gate
--      (the closed in-code regex: rules?/no exceptions/rule of thumb/to live by) is now the
--      regex ∪ the GROWN class, and every register firing (and every cue-led sentence no
--      frame captured) records the MATCHED cue surface on the growth rail — so a tenant that
--      declares rules under cue words the regex never named ("guideline:", "standing
--      policy:") grows its own cue vocabulary freq-gated. The cue class carries the frame
--      GATE only — never a mint by itself; the frame regexes own the value and the negation
--      scan-back contract is unchanged.
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category
-- and the provisioning seeder blanket-copies ALL public.linguistic_cues categories into every
-- NEW tenant — new tenants inherit automatically. This migration only (1) seeds the two new
-- categories into public and (2) fans them out to EXISTING tenant schemas (the 308 pattern).
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/308).
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

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the two new classes
-- ============================================================================
-- 'cue' is matched lowercase; medical_relation_adj members are consumed as ADJECTIVE
-- LEMMAS (the ``<adj> to <NP>`` frame's complement lemma); rule_register_cue members are
-- consumed as whole PHRASE surfaces present in the sentence (word-boundary). Membership in
-- medical_relation_adj drives a SHELF MAPPING (has_allergy), so the seed stays MINIMAL —
-- the one measured member — and the class grows only through the freq-gated rail.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('allergic', 'medical_relation_adj',
   'Medical-relation adjective: the <adj> to <NP> first-person frame files has_allergy (the declared shelf) instead of a novel rel',
   'I''m allergic to penicillin', 'seed_bug_family_7', 0.92),
  ('rule',        'rule_register_cue', 'Rule-declaration cue surface (the register arm''s gate; frames own the value)', 'House rule for the lab: always snapshot before an upgrade', 'seed_bug_family_7', 0.92),
  ('rules',       'rule_register_cue', 'Rule-declaration cue surface', 'Personal rules that paid for themselves: …', 'seed_bug_family_7', 0.9),
  ('no exceptions', 'rule_register_cue', 'Rule-declaration cue surface (phrase)', '… before an upgrade, no exceptions.', 'seed_bug_family_7', 0.9),
  ('rule of thumb', 'rule_register_cue', 'Rule-declaration cue surface (phrase)', 'Rule of thumb: measure twice.', 'seed_bug_family_7', 0.9),
  ('to live by',    'rule_register_cue', 'Rule-declaration cue surface (phrase)', 'A rule to live by: never deploy Friday.', 'seed_bug_family_7', 0.9)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.linguistic_cues (
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
            )
        $ddl$, _schema);

        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category IN ('medical_relation_adj', 'rule_register_cue')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 316: medical_relation_adj + rule_register_cues seeded into %', _schema;
    END LOOP;
END $$;
