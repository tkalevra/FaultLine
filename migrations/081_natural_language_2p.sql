-- Migration 081: Second-person recall prose (natural_language_2p)
-- Date: 2026-06-11
-- Purpose: Add rel_types.natural_language_2p — a SECOND-PERSON template used at
--          render time ONLY when the SUBJECT slot resolves to "you" (the querying
--          user). The 3p templates ("X is the parent of Y") break when X == "you"
--          ("you is the parent of…"); the 2p form bakes the second-person subject
--          in and keeps the object slot Y ("You are the parent of Y").
--
-- Presentation only: no stored facts change. The render path (convert_to_prose)
-- picks the 2p template when subject=="you" AND natural_language_2p IS NOT NULL,
-- else falls back to the 3p template (with a minimal agreement fixup).
--
-- Per-tenant: the column + backfill MUST reach every faultline_% schema — at
-- runtime search_path EXCLUDES public, so a public-only change is a silent no-op.
-- Mirrors the 073 DO-block loop with IF EXISTS guards.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS; backfill only WHERE natural_language_2p
-- IS NULL (never clobbers a previously-set/hand-tuned value).

-- ============================================================================
-- Part 1: Public schema
-- ============================================================================

ALTER TABLE public.rel_types ADD COLUMN IF NOT EXISTS natural_language_2p TEXT;

-- Backfill the 2p forms (subject baked as "you/your", object kept as Y slot).
-- Generated from the live 3p natural_language values. Only fills NULLs.
UPDATE public.rel_types AS r SET natural_language_2p = v.nl2p
FROM (VALUES
    ('affected_body_part', 'You are affected in body part Y'),
    ('age',                'You are Y years old'),
    ('also_known_as',      'You are also known as Y'),
    ('backs_up',           'You back up Y'),
    ('belongs_to',         'You belong to Y'),
    ('born_in',            'You were born in Y'),
    ('born_on',            'You were born on Y'),
    ('builds',             'You build Y'),
    ('child_of',           'You are the child of Y'),
    ('connects_to',        'You connect to Y'),
    ('contains',           'You contain Y'),
    ('created_by',         'You were created by Y'),
    ('defines',            'You define Y'),
    ('depends_on',         'You depend on Y'),
    ('dislikes',           'You dislike Y'),
    ('educated_at',        'You were educated at Y'),
    ('fqdn',               'You have FQDN Y'),
    ('friend_of',          'You are a friend of Y'),
    ('has_allergy',        'You have allergy Y'),
    ('has_email',          'You have email Y'),
    ('has_fqdn',           'You have FQDN Y'),
    ('has_gender',         'Your gender is Y'),
    ('has_hostname',       'You have hostname Y'),
    ('has_injury',         'You have an injury Y'),
    ('has_ip',             'You have IP address Y'),
    ('has_mac',            'You have MAC address Y'),
    ('has_medical_condition', 'You have medical condition Y'),
    ('has_medication',     'You take medication Y'),
    ('has_os',             'You have operating system Y'),
    ('has_pet',            'You have a pet that is Y'),
    ('has_phone',          'You have phone Y'),
    ('has_subnet',         'You have subnet Y'),
    ('has_symptom',        'You have symptom Y'),
    ('has_url',            'You have URL Y'),
    ('has_uuid',           'You have UUID Y'),
    ('height',             'Your height is Y'),
    ('hostname',           'You have hostname Y'),
    ('hosts',              'You host Y'),
    ('instance_of',        'You are an instance of Y'),
    ('instantiates',       'You instantiate Y'),
    ('ip_address',         'You have IP address Y'),
    ('is_a',               'You are a type of Y'),
    ('knows',              'You know Y'),
    ('leads',              'You lead Y'),
    ('likes',              'You like Y'),
    ('links',              'You link to Y'),
    ('listens_on',         'You listen on Y'),
    ('lives_at',           'You live at Y'),
    ('lives_in',           'You live in Y'),
    ('located_at',         'You are located at Y'),
    ('located_in',         'You are located in Y'),
    ('managed_by',         'You are managed by Y'),
    ('manages',            'You manage Y'),
    ('member_of',          'You are a member of Y'),
    ('met',                'You have met Y'),
    ('monitors',           'You monitor Y'),
    ('nationality',        'Your nationality is Y'),
    ('occupation',         'Your occupation is Y'),
    ('owns',               'You own Y'),
    ('parent_of',          'You are the parent of Y'),
    ('part_of',            'You are a part of Y'),
    ('pref_name',          'You go by Y'),
    ('prefers',            'You prefer Y'),
    ('protects',           'You protect Y'),
    ('provides',           'You provide Y'),
    ('related_to',         'You are related to Y'),
    ('represents',         'You represent Y'),
    ('resides_in',         'You reside in Y'),
    ('runs',               'You run Y'),
    ('same_as',            'You and Y are the same entity'),
    ('sibling_of',         'You and Y are siblings'),
    ('spouse',             'You and Y are spouses/partners'),
    ('stores',             'You store Y'),
    ('subclass_of',        'You are a subclass of Y'),
    ('weight',             'Your weight is Y'),
    ('works_for',          'You work for Y')
) AS v(rel_type, nl2p)
WHERE r.rel_type = v.rel_type
  AND r.natural_language_2p IS NULL;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas)
-- ============================================================================
-- Add the column + copy the freshly-backfilled 2p forms down from public for
-- every rel_type that exists in the tenant schema. Idempotent on both counts.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            -- 2a. Add the column if missing.
            EXECUTE format(
                'ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS natural_language_2p TEXT',
                _schema
            );

            -- 2b. Backfill from public.rel_types (single source of truth), only
            --     where the tenant value is still NULL — never clobber.
            EXECUTE format(
                'UPDATE %I.rel_types AS r
                    SET natural_language_2p = p.natural_language_2p
                   FROM public.rel_types AS p
                  WHERE r.rel_type = p.rel_type
                    AND r.natural_language_2p IS NULL
                    AND p.natural_language_2p IS NOT NULL',
                _schema
            );
        END IF;
    END LOOP;
END $$;
