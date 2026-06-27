-- Migration 117: linguistic_cues — seed the kinship_gender cue class onto the SAME rail
-- Date: 2026-06-27
--
-- WHY
-- ---
-- The unified NAMED-INSTANCE binding chain ("a son Alex 19", "a daughter Robin 10", "a dog
-- named Rex", "a friend Sam" — ONE connector-agnostic detector) resolves, FOR EACH bound
-- (ProperName ↔ common-noun Type), the relation from the TYPE's category via METADATA — the ONLY
-- place domains differ. For a KINSHIP type the chain already mints the specific kin rel (son→child_of)
-- from the kinship_noun row's `description` (migration 109). It ALSO mints the gender the role
-- intrinsically carries (son→male, daughter→female) — and that gender must be DB-HELD + per-tenant +
-- growable on the SAME rail, NOT an in-code `if noun=='son'` literal.
--
-- This migration seeds a NEW keyed category 'kinship_gender' onto public.linguistic_cues: `cue` = the
-- kinship noun lemma (lowercase), `description` = the GENDER token (male/female). resolve_kinship_
-- gender_map() (linguistic_cue_overlay) reads (cue, description) back as a {noun: gender} dict, the
-- SAME ContextVar-bound per-tenant overlay the kinship_noun / unit_scalar / thin_type maps use. The
-- gender routes to the SCALAR `has_gender` rel (tail_types={SCALAR}) → entity_attributes.
--
-- ONLY the GENDERED kin roles appear. A GENDER-NEUTRAL kin role (child / parent / sibling / spouse /
-- partner / cousin) is INTENTIONALLY ABSENT so no gender is fabricated where the language does not
-- state one. The in-code map remains as the DB-DOWN CODE-FALLBACK SEED only
-- (linguistic_cue_overlay._BOOTSTRAP_KINSHIP_GENDER_MAP) — the live authority is the DB.
--
-- NO DDL CHANGE: 105 created public.linguistic_cues general-by-category and the provisioning seeder
-- blanket-copies ALL public categories into every NEW tenant — new tenants inherit kinship_gender
-- automatically. This migration only (1) seeds public and (2) fans out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or wait
-- the 5s TTL.

-- Guard: if migration 105 has not run, create the table in public (same idempotent DDL).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the kinship_gender class
-- ============================================================================
-- `cue` = kinship noun lemma; `description` = gender (male/female). Only GENDERED roles; neutral kin
-- (child/parent/sibling/spouse/partner/cousin) intentionally omitted (no fabricated gender). All
-- literals single-quoted ('' escaping), per the 109 lesson (double quotes are identifiers, not text).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('son',         'kinship_gender', 'male',   'a son Alex',        'seed_kinship_gender', 0.95),
  ('daughter',    'kinship_gender', 'female', 'a daughter Robin', 'seed_kinship_gender', 0.95),
  ('mother',      'kinship_gender', 'female', 'my mother',    'seed_kinship_gender', 0.95),
  ('father',      'kinship_gender', 'male',   'my father',     'seed_kinship_gender', 0.95),
  ('mom',         'kinship_gender', 'female', 'my mom',             'seed_kinship_gender', 0.92),
  ('dad',         'kinship_gender', 'male',   'my dad',             'seed_kinship_gender', 0.92),
  ('sister',      'kinship_gender', 'female', 'my sister',    'seed_kinship_gender', 0.93),
  ('brother',     'kinship_gender', 'male',   'my brother',     'seed_kinship_gender', 0.93),
  ('wife',        'kinship_gender', 'female', 'my wife',      'seed_kinship_gender', 0.93),
  ('husband',     'kinship_gender', 'male',   'my husband',         'seed_kinship_gender', 0.93),
  ('uncle',       'kinship_gender', 'male',   'my uncle',           'seed_kinship_gender', 0.88),
  ('aunt',        'kinship_gender', 'female', 'my aunt',            'seed_kinship_gender', 0.88),
  ('grandmother', 'kinship_gender', 'female', 'my grandmother',     'seed_kinship_gender', 0.88),
  ('grandfather', 'kinship_gender', 'male',   'my grandfather',     'seed_kinship_gender', 0.88),
  ('grandma',     'kinship_gender', 'female', 'my grandma',         'seed_kinship_gender', 0.85),
  ('grandpa',     'kinship_gender', 'male',   'my grandpa',         'seed_kinship_gender', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- social_role: the PERSON-social-role noun → rel_type MAP for the named-instance binding chain.
-- "a friend Sam" → friend → (sam, friend_of, user); "my colleague" → knows. A PERSON
-- introduced by a social role binds to a SOCIAL tie (friend_of / knows), NEVER ``owns`` (a person is
-- not owned). A role OUTSIDE this map falls to a generic ``has_role`` slot (no fabricated tie).
-- resolve_social_role_map() reads (cue, description) as {noun: rel_type}. Mirrors
-- _BOOTSTRAP_SOCIAL_ROLE_MAP.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('friend',       'social_role', 'friend_of', 'a friend Sam',     'seed_social_role', 0.92),
  ('colleague',    'social_role', 'knows',     'my colleague',   'seed_social_role', 0.85),
  ('coworker',     'social_role', 'knows',     'my coworker',        'seed_social_role', 0.82),
  ('neighbour',    'social_role', 'knows',     'my neighbour',       'seed_social_role', 0.82),
  ('neighbor',     'social_role', 'knows',     'my neighbor',        'seed_social_role', 0.82),
  ('acquaintance', 'social_role', 'knows',     'an acquaintance',    'seed_social_role', 0.80),
  ('classmate',    'social_role', 'knows',     'my classmate',       'seed_social_role', 0.80),
  ('roommate',     'social_role', 'knows',     'my roommate',        'seed_social_role', 0.80),
  ('boss',         'social_role', 'knows',     'my boss',            'seed_social_role', 0.80),
  ('manager',      'social_role', 'knows',     'my manager',         'seed_social_role', 0.80)
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
            WHERE category IN ('kinship_gender', 'social_role')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 117: kinship_gender cues seeded into %', _schema;
    END LOOP;
END $$;
