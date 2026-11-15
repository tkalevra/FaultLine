-- Migration 329: entity-centric filing — the L4 SPACE rows + the space head classes.
-- Date: 2026-11-15
--
-- WHY
-- ---
-- The wound-map doctrine lane (the owner's WORLD MODEL THESIS): entities exist in MULTIPLES
-- of L4 spaces simultaneously; things stand as entities with components as slots; NOTHING
-- flattens into user attrs. The pet ladder (Luna: instance_of + has_pet + the pets
-- taxonomy) is the correct shape — this migration gives the GENERALIZED filing arms the
-- same ground to stand on for the thing spaces the ladder never owned:
--   • handbag  → possessions   (member via `owns`)
--   • volunteer event → events (member via `participated_in`, migration 100's seeded rel)
--   • playlists/collections → collections ⊂ possessions (the migration 087 nesting pattern)
-- The deriver's entity-centric arms consult these rows at mint as a LOOKUP (the /expand
-- consult): when the map holds the space, ITS defining rel names the member edge. The
-- in-code _EC_SPACE_SEED_FLOOR (linguistics.py) is this migration's DB-DOWN mirror.
--
-- SHAPE (same conventions as 019/087/121/308):
--   1. entity_taxonomies rows for `possessions`, `collections`, `events` in public (template)
--      — NO seeded member words (the ladder self-builds via what-is); the rows carry only
--      the coarse member CLASSES and the EXISTING seeded defining rels (owns/has_pet/
--      participated_in — no new rel_types minted here).
--   2. linguistic_cues seeds for the `collection_noun` and `event_noun` head classes
--      (⚠️ FLAGGED BOUNDED LEXICAL CLASSES, honestly documented — collection-ness and
--      event-ness cannot be made purely structural; mirrors the pet_noun seed, migration 308).
--   3. Fan-out to EXISTING tenant schemas (the 087 DO loop pattern). New tenants inherit
--      via the provisioning seeder's blanket copy of public.
-- Idempotent: ON CONFLICT DO NOTHING + guarded UPDATEs. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay caches
-- (GET /internal/refresh-intent-pattern-caches?schema=...) or wait the 5s TTL.

-- ============================================================================
-- Part 1: entity_taxonomies — the doctrine's thing spaces (public = seed source)
-- ============================================================================

-- possessions: things the subject OWNS (the map100 handbag class)
INSERT INTO public.entity_taxonomies
    (taxonomy_name, description, member_entity_types, rel_types_defining_group,
     has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies, source)
VALUES
    ('possessions',
     'A subject''s possessions — concrete things linked by owns; nests the collections group',
     ARRAY['Object'],
     ARRAY['owns']::TEXT[],
     true,
     ARRAY['owns', 'instance_of', 'subclass_of']::TEXT[],
     true,
     ARRAY['collections']::TEXT[],
     'seeded')
ON CONFLICT (taxonomy_name) DO NOTHING;

-- collections: countable plural collections (the map100 spotify_playlist_count class);
-- the SUB-space of possessions (a collection IS a possession whose members are things)
INSERT INTO public.entity_taxonomies
    (taxonomy_name, description, member_entity_types, rel_types_defining_group,
     has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies, source)
VALUES
    ('collections',
     'A subject''s countable collections — playlists, records, books; the plural thing space',
     ARRAY['Object'],
     ARRAY['owns']::TEXT[],
     true,
     ARRAY['owns', 'count', 'instance_of']::TEXT[],
     true,
     ARRAY[]::TEXT[],
     'seeded')
ON CONFLICT (taxonomy_name) DO NOTHING;

-- events: happenings the subject participates in (the map100 volunteer_event_date class);
-- the defining rel is migration 100's seeded `participated_in` (no new rel minted)
INSERT INTO public.entity_taxonomies
    (taxonomy_name, description, member_entity_types, rel_types_defining_group,
     has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies, source)
VALUES
    ('events',
     'Events the subject participates in — dinners, fundraisers, concerts; the date is the event''s own slot',
     ARRAY['Event'],
     ARRAY['participated_in']::TEXT[],
     true,
     ARRAY['participated_in', 'attended_event_at', 'event_date']::TEXT[],
     true,
     ARRAY[]::TEXT[],
     'seeded')
ON CONFLICT (taxonomy_name) DO NOTHING;

-- ============================================================================
-- Part 2: linguistic_cues — the collection_noun / event_noun head classes (public)
-- ============================================================================
-- Mirrors _BOOTSTRAP_COLLECTION_NOUNS / _BOOTSTRAP_EVENT_NOUNS exactly (the frozensets are
-- the DB-DOWN floors; these rows are the DB authority). Membership is corroborated
-- downstream by the frame grammar (the count chain / the naming binding / the claimant
-- post-pass) AND the exclusion ladders (person / location / inalienable-body / animal never
-- admit), so a stray noun can never mint a thing on its own.
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

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('playlist',          'collection_noun', 'Countable-collection head noun', 'I have 20 playlists on Spotify',                       'seed_entity_centric', 0.92),
  ('playlists',         'collection_noun', 'Countable-collection head noun (plural)', 'I have 20 playlists on Spotify',              'seed_entity_centric', 0.92),
  ('collection',        'collection_noun', 'Countable-collection head noun', 'I own a collection of vinyl records',                  'seed_entity_centric', 0.90),
  ('record',            'collection_noun', 'Countable-collection head noun', 'I have 40 records in my collection',                   'seed_entity_centric', 0.85),
  ('records',           'collection_noun', 'Countable-collection head noun (plural)', 'I have 40 records',                           'seed_entity_centric', 0.85),
  ('book',              'collection_noun', 'Countable-collection head noun', 'I have 200 books at home',                             'seed_entity_centric', 0.85),
  ('books',             'collection_noun', 'Countable-collection head noun (plural)', 'I have 200 books',                            'seed_entity_centric', 0.85),
  ('photo',             'collection_noun', 'Countable-collection head noun', 'I have 3000 photos in my library',                     'seed_entity_centric', 0.85),
  ('photos',            'collection_noun', 'Countable-collection head noun (plural)', 'I have 3000 photos',                          'seed_entity_centric', 0.85),
  ('recipe',            'collection_noun', 'Countable-collection head noun', 'I have 60 recipes saved',                              'seed_entity_centric', 0.85),
  ('recipes',           'collection_noun', 'Countable-collection head noun (plural)', 'I have 60 recipes',                           'seed_entity_centric', 0.85),
  ('comic',             'collection_noun', 'Countable-collection head noun', 'I have 150 comics boxed and boarded',                  'seed_entity_centric', 0.80),
  ('comics',            'collection_noun', 'Countable-collection head noun (plural)', 'I have 150 comics',                           'seed_entity_centric', 0.80),
  ('event',             'event_noun',      'Event head noun', 'I volunteered at the event on February 14th',          'seed_entity_centric', 0.90),
  ('dinner',            'event_noun',      'Event head noun', 'I volunteered at the fundraising dinner on Valentine''s Day', 'seed_entity_centric', 0.85),
  ('fundraiser',        'event_noun',      'Event head noun', 'I helped at the fundraiser on February 14th',          'seed_entity_centric', 0.88),
  ('fundraising dinner','event_noun',      'Event head noun (phrase cue)', 'I volunteered at a fundraising dinner on February 14th',  'seed_entity_centric', 0.88),
  ('gala',              'event_noun',      'Event head noun', 'I attended the gala on February 14th',                 'seed_entity_centric', 0.85),
  ('party',             'event_noun',      'Event head noun', 'I hosted a party on Saturday',                         'seed_entity_centric', 0.80),
  ('concert',           'event_noun',      'Event head noun', 'I loved the concert I was at on February 14th',        'seed_entity_centric', 0.85),
  ('festival',          'event_noun',      'Event head noun', 'I volunteered at the festival last month',             'seed_entity_centric', 0.85),
  ('recital',           'event_noun',      'Event head noun', 'I attended the recital on Friday',                     'seed_entity_centric', 0.80),
  ('auction',           'event_noun',      'Event head noun', 'I helped run the auction on Saturday',                 'seed_entity_centric', 0.80),
  ('wedding',           'event_noun',      'Event head noun', 'I attended the wedding in June',                       'seed_entity_centric', 0.80),
  ('conference',        'event_noun',      'Event head noun', 'I spoke at the conference on May 3rd',                 'seed_entity_centric', 0.80),
  ('meetup',            'event_noun',      'Event head noun', 'I organize a meetup every month',                      'seed_entity_centric', 0.80),
  ('volunteer event',   'event_noun',      'Event head noun (phrase cue)', 'My volunteer event happens on February 14th',             'seed_entity_centric', 0.88)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 3: fan out to EXISTING tenant schemas (mirrors migration 087's DO loop)
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- the three space rows (explicit columns, ON CONFLICT DO NOTHING)
        EXECUTE format($seed$
            INSERT INTO %I.entity_taxonomies
                (taxonomy_name, description, member_entity_types, rel_types_defining_group,
                 has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies, source)
            SELECT taxonomy_name, description, member_entity_types, rel_types_defining_group,
                   has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies,
                   COALESCE(source, 'seeded')
            FROM public.entity_taxonomies
            WHERE taxonomy_name IN ('possessions', 'collections', 'events')
            ON CONFLICT (taxonomy_name) DO NOTHING
        $seed$, _schema);

        -- the space head classes (the cue table is created per-tenant by migration 105's
        -- fan-out; CREATE IF NOT EXISTS keeps this migration self-sufficient on any schema
        -- that predates it)
        EXECUTE format($cue$
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
        $cue$, _schema);

        EXECUTE format($cue2$
            INSERT INTO %I.linguistic_cues
                (cue, category, description, example_text, source, global_confidence)
            SELECT cue, category, description, example_text, source, global_confidence
            FROM public.linguistic_cues
            WHERE category IN ('collection_noun', 'event_noun')
            ON CONFLICT (cue, category) DO NOTHING
        $cue2$, _schema);

        RAISE NOTICE 'Migration 329: entity-centric spaces seeded into %', _schema;
    END LOOP;
END $$;
