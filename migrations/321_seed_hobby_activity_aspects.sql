-- Migration 321: seed the HOBBY ACTIVITY class members into the aspect_rel_affinity cue
-- class (the s1 wire-drive hobby two-seam fix, SERVE half, 2026-11-13).
--
-- WHY
-- ---
-- The s1 verdict (DEV/BUGS/wire-drive-s1-20261113/S1-WIRE-FIX-VERDICT.md, hobby row)
-- measured the two-seam miss: "On weekends I like to ride my Vespa around town." filed
-- NO activity row (the like-matrix swallowed the catenative "ride my Vespa") and the
-- recall abstained honest-empty. The capture half landed as linguistics'
-- FRONT_ADVERBIAL_ACTIVITY_FRAME (rows (user, ride, vespa) + the cadence surface
-- (user, ride_on, weekends)). THE SERVE HALF needs the ASPECT-CLASS bridge: the
-- canonical ask "what do I like to do on weekends?" never surfaces the row's own verb
-- ("ride"), so no query-token binding can lay the row into the walk's scope — the s1
-- drive measured the fetch-all lane retrieving 27 rows and the lexical topic gate
-- dropping ALL of them (after=0). The bridge is the SAME growable aspect_rel_affinity
-- class migration 319 created (cue = the aspect word, description = the canonical rel):
-- the ACTIVITY verbs join the class (ride/rides/riding → ride; play/plays/playing →
-- play; restore/restores/restoring → restore; bare do → do), and main.py's
-- SPEAKER-anchored hobby arm (RECALL_HOBBY_ACTIVITY_ADMISSION) admits the class's
-- VALUES into aspect_bound_rels when the first-person activity-ask frame fires AND the
-- store holds LIVE rows on them — metadata-coupled: no live row → no admission → the
-- abstention stands honestly.
--
-- The bare "do" surface is seeded deliberately ("what do I DO on weekends?" is the
-- canonical ask); "does"/"doing" are NOT — the interrogative auxiliary "does" ("what
-- does my hypervisor run?") is the same word and must never mint a rel out of an
-- auxiliary. Membership drives a LIVE-ROW-COUPLED admission; the seed stays MINIMAL
-- (the measured members only).
--
-- NO DDL CHANGE: the aspect_rel_affinity category exists via migration 319 (public seed
-- source + per-tenant fan-out). This migration seeds public (the template) and fans the
-- NEW rows out to EXISTING tenant schemas (the 308/316/319 pattern — the category copy
-- carries every active aspect_rel_affinity row, the 319-seeded run/runs/running included;
-- ON CONFLICT DO NOTHING keeps it idempotent). Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache
-- (GET /internal/refresh-intent-pattern-caches) or wait the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/308/316/319).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the hobby activity members
-- ============================================================================
-- 'cue' is matched as a whole WORD in the query; 'description' carries the canonical
-- rel_type the aspect binds to. Consumed by the speaker-anchored hobby arm (class =
-- the VALUES) and the entity-anchored arm (the query's own words), unchanged.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('ride',      'aspect_rel_affinity', 'ride',    'what do I ride on weekends?',             'seed_s1_hobby_20261113', 0.92),
  ('rides',     'aspect_rel_affinity', 'ride',    'what do I ride on weekends?',             'seed_s1_hobby_20261113', 0.92),
  ('riding',    'aspect_rel_affinity', 'ride',    'what do I like riding on weekends?',      'seed_s1_hobby_20261113', 0.92),
  ('play',      'aspect_rel_affinity', 'play',    'what do I play on weekends?',             'seed_s1_hobby_20261113', 0.92),
  ('plays',     'aspect_rel_affinity', 'play',    'what do I play on weekends?',             'seed_s1_hobby_20261113', 0.92),
  ('playing',   'aspect_rel_affinity', 'play',    'what do I like playing on weekends?',     'seed_s1_hobby_20261113', 0.92),
  ('restore',   'aspect_rel_affinity', 'restore', 'what do I restore on weekends?',          'seed_s1_hobby_20261113', 0.92),
  ('restores',  'aspect_rel_affinity', 'restore', 'what do I restore on weekends?',          'seed_s1_hobby_20261113', 0.92),
  ('restoring', 'aspect_rel_affinity', 'restore', 'what do I like restoring on weekends?',   'seed_s1_hobby_20261113', 0.92),
  ('do',        'aspect_rel_affinity', 'do',      'what do I like to do on weekends?',       'seed_s1_hobby_20261113', 0.92)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- (the 308/316/319 fan-out; the guarded loop skips a partially-provisioned seat instead
-- of aborting the whole DO block)
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
        BEGIN
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
        EXCEPTION
            WHEN undefined_table THEN NULL;
            WHEN OTHERS THEN NULL;
        END;

        BEGIN
            EXECUTE format($seed$
                INSERT INTO %I.linguistic_cues
                    (cue, category, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     source, is_active, archived_at, last_matched_at)
                SELECT cue, category, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       source, is_active, archived_at, last_matched_at
                FROM public.linguistic_cues
                WHERE category IN ('aspect_rel_affinity')
                ON CONFLICT (cue, category) DO NOTHING
            $seed$, _schema);
        EXCEPTION
            WHEN undefined_table THEN NULL;
            WHEN OTHERS THEN NULL;
        END;
    END LOOP;
END
$$;
