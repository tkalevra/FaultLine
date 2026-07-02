-- Migration 126: linguistic_cues — seed the SHELL-NOUN (generic abstract anaphoric head) cue class.
-- Date: 2026-07-01
--
-- WHY
-- ---
-- A dense multi-sentence turn describes ONE topic across several sentences ("CVE-2026-1234 is a
-- critical vulnerability. The FLAW has been exploited. Oracle released a patch."). The cross-sentence
-- discourse-topic coref (derive_sentence_facts._topic_definite_subject) already rebinds a subject
-- PRONOUN ("it") and a DEFINITE subject NP whose head is the topic's EXACT type noun ("the
-- vulnerability") back to the topic — so the description consolidates onto ONE entity. But a later
-- sentence often re-refers to the topic with a GENERIC SHELL noun ("the FLAW"/"the RULING"/"the
-- CONDITION") that is NOT the topic's exact type noun, and GLiNER2 does not coarse-match it (flaw ≉
-- vulnerability, ruling ≉ case). That sentence's facts then ISLAND on the shell noun instead of the
-- topic ("(flaw, exploited_by, ShinyHunters)" instead of "(CVE, exploited_by, ShinyHunters)").
--
-- This cue class supplies the SHELL-NOUN inventory the coref consults: a DEFINITE subject NP whose head
-- is in this class, with no closer antecedent and one unambiguous topic, binds to the topic. Shell
-- nouns are TYPE-AGNOSTIC re-reference devices (a shell can re-refer to a Person topic — "the condition
-- worsened" → the patient), so the bind is not gated on type match; it rides the definiteness +
-- single-topic + no-closer-antecedent guards already in the coref.
--
-- SUBJECT-AGNOSTIC (NOT a domain list): these are the GENERIC abstract/shell nouns of English
-- discourse (Schmid's "shell nouns" / Halliday's general nouns) that recur across EVERY domain — a CVE,
-- a court ruling, a diagnosis, a device fault all get re-referred as "the issue"/"the matter"/"the
-- thing". No secops/legal/clinical surface appears here.
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the coref's dependency/POS guards (nsubj subject,
-- determiner definiteness, no-closer-antecedent, single-topic). Only the SHELL-NOUN lemma recognition
-- is DB data that grows. NO noun list in .py logic — the discriminator lives in this DB cue class. The
-- in-code linguistic_cue_overlay._BOOTSTRAP_SHELL_NOUNS / linguistics._SHELL_NOUN_LEMMAS frozensets
-- REMAIN only as the DB-DOWN code-fallback seed (fail-safe; never lose the bind on a pre-migration /
-- unwarmed-overlay turn).
--
-- NO DDL CHANGE: migration 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder (schema_manager.py) blanket-copies public.linguistic_cues into every NEW tenant
-- (shell_noun is a GRAMMAR/GENERIC class, NOT in the domain-flavored carve-out list
-- social_role/problem_noun/thin_type) — so new tenants inherit this class automatically. This migration
-- only (1) seeds the new category into public and (2) fans it out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL. A FRESH provision (or migration run) is required for the class to appear in a tenant.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/108/…).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the shell-noun class
-- ============================================================================
-- `cue` is matched against the spaCy head-noun lemma (lowercase) of a DEFINITE later-sentence subject.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('flaw',      'shell_noun', 'Generic abstract/shell anaphoric head; a definite "the flaw" re-refers to the topic', 'The flaw has been exploited',   'seed_shell_noun', 0.90),
  ('issue',     'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The issue was resolved',        'seed_shell_noun', 0.90),
  ('problem',   'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The problem persisted',         'seed_shell_noun', 0.88),
  ('matter',    'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The matter was closed',         'seed_shell_noun', 0.85),
  ('condition', 'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The condition worsened',        'seed_shell_noun', 0.88),
  ('situation', 'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The situation escalated',       'seed_shell_noun', 0.82),
  ('case',      'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The case was dismissed',        'seed_shell_noun', 0.82),
  ('finding',   'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The finding was confirmed',     'seed_shell_noun', 0.82),
  ('defect',    'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The defect was patched',        'seed_shell_noun', 0.85),
  ('fault',     'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The fault was traced',          'seed_shell_noun', 0.82),
  ('entity',    'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The entity was flagged',        'seed_shell_noun', 0.78),
  ('item',      'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The item was reviewed',         'seed_shell_noun', 0.75),
  ('thing',     'shell_noun', 'Generic abstract/shell anaphoric head',                                               'The thing broke again',         'seed_shell_noun', 0.70),
  ('ruling',    'shell_noun', 'Generic abstract/shell anaphoric head (outcome shell)',                              'The ruling overruled Baker',    'seed_shell_noun', 0.85),
  ('decision',  'shell_noun', 'Generic abstract/shell anaphoric head (outcome shell)',                              'The decision was appealed',     'seed_shell_noun', 0.82),
  ('incident',  'shell_noun', 'Generic abstract/shell anaphoric head (outcome shell)',                              'The incident was contained',    'seed_shell_noun', 0.80)
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

        EXECUTE format($ix1$
            CREATE INDEX IF NOT EXISTS idx_linguistic_cues_active
                ON %I.linguistic_cues(is_active, category)
        $ix1$, _schema);
        EXECUTE format($ix2$
            CREATE INDEX IF NOT EXISTS idx_linguistic_cues_category
                ON %I.linguistic_cues(category)
        $ix2$, _schema);

        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category = 'shell_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 126: shell_noun cues seeded into %', _schema;
    END LOOP;
END $$;
