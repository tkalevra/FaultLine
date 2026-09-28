-- Migration 187: linguistic_cues — seed the TIME units into the unit_scalar cue class (→ 'duration')
-- Date: 2026-07-17
--
-- WHY
-- ---
-- LongMemEval q118b2229 — "How long is my daily commute to work?" (gold "45 minutes each way") —
-- returned EMPTY. The possessed-measure duration ("my daily commute takes 45 minutes each way") was
-- captured only on the ISLAND entity "commute", unreachable from the user anchor, so recall found
-- nothing. The possessed-attribute scalar detector (_attr_scalar_binding, src/extraction/linguistics
-- .py) now ALSO owns the measure-verb frame "my X takes/lasts N <unit>" and lands the measure as a
-- USER-anchored scalar — the SAME recallable shape as "my address is 123 Main St".
--
-- The recognition GATE for that frame is: the direct object is a NUM-quantified UNIT noun whose lemma
-- is in the unit_scalar cue map. The map already carries the length/weight/age primitives (foot →
-- height, pound → weight, year → age) but had NO TIME units — so a duration measure was not
-- recognized. This migration adds the TIME units as measurement primitives mapping to 'duration'.
--
-- These are MEASUREMENT PRIMITIVES on the SAME bounded lexical rail as foot / pound / year — NOT
-- domain literals: the map only asserts "a NUM-quantified <time-unit> is a measured DURATION", the
-- grammar/shape decides everything else. `cue` = the spaCy noun lemma (lowercase); `description` =
-- the scalar rel_type ('duration'); resolve_unit_scalar_map() reads them as {unit: rel_type}.
--
-- Mirrors the DB-DOWN code-fallback in linguistic_cue_overlay._BOOTSTRAP_UNIT_SCALAR_MAP (the TIME
-- units were added there in the same change). NO DDL change: migration 105 created public
-- .linguistic_cues (+ per-tenant), and the provisioning seeder (schema_manager.py) blanket-copies ALL
-- public.linguistic_cues categories into every NEW tenant — so new tenants inherit these rows
-- automatically. This migration only (1) seeds public and (2) fans out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING.
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
-- `cue` = the spaCy noun lemma (lowercase); `description` = the scalar rel_type ('duration').
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('second', 'unit_scalar', 'duration', 'the call took 90 seconds',        'seed_unit_scalar', 0.85),
  ('minute', 'unit_scalar', 'duration', 'my commute takes 45 minutes',     'seed_unit_scalar', 0.88),
  ('hour',   'unit_scalar', 'duration', 'the flight is 3 hours',           'seed_unit_scalar', 0.88),
  ('day',    'unit_scalar', 'duration', 'the trip lasts 5 days',           'seed_unit_scalar', 0.82),
  ('week',   'unit_scalar', 'duration', 'the course runs 6 weeks',         'seed_unit_scalar', 0.82),
  ('month',  'unit_scalar', 'duration', 'the lease is 12 months',          'seed_unit_scalar', 0.82)
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
              ('second', 'unit_scalar', 'duration', 'the call took 90 seconds',    'seed_unit_scalar', 0.85),
              ('minute', 'unit_scalar', 'duration', 'my commute takes 45 minutes', 'seed_unit_scalar', 0.88),
              ('hour',   'unit_scalar', 'duration', 'the flight is 3 hours',       'seed_unit_scalar', 0.88),
              ('day',    'unit_scalar', 'duration', 'the trip lasts 5 days',       'seed_unit_scalar', 0.82),
              ('week',   'unit_scalar', 'duration', 'the course runs 6 weeks',     'seed_unit_scalar', 0.82),
              ('month',  'unit_scalar', 'duration', 'the lease is 12 months',      'seed_unit_scalar', 0.82)
            ON CONFLICT (cue, category) DO NOTHING
        $f$, _schema);
    END LOOP;
END $$;
