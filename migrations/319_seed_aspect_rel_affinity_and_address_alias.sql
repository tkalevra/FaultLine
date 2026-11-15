-- Migration 319: seed the ASPECT-REL-AFFINITY cue class + the ADDRESS rel-type aliases
-- (PROD-BUG-1 + PROD-BUG-4 serve gaps, measured on the live wire, 2026-11-12).
--
-- WHY
-- ---
-- The honest prod wire drive (DEV/BUGS/family-smoke-20261111/PROD-DRIVE-RESULTS.md, the
-- 2/8 formal verdict) measured two CLASS-shaped serve gaps, reproduced on the pre-prod
-- baseline seat (throwaway drive harness, fresh names):
--
--   1. aspect_rel_affinity (PROD-BUG-1, HIGH): "My homelab hypervisor is XCP-ng running on
--      an old Dell tower in the basement." filed (xcp-ng, run_on, old dell tower …) LIVE —
--      the LLM lane's novel rel — while "what does my homelab hypervisor run?" served the
--      located_in fragment on EVERY phrasing. The question's own verb "run" bound NOTHING
--      (no rel_type named ``run``, no rel_type_aliases row — measured on the drive seat:
--      the only alias row was lives→lives_in), so ``run_on`` never entered the walk's
--      scope and the row sat dark. The fix maps the aspect word onto the canonical rel as
--      SEEDED GROWABLE VOCABULARY (the migration-308/316 discipline): the class is
--      ``aspect_rel_affinity`` (cue = the question's aspect word, description = the
--      canonical rel_type — the same desc-carries-canonical shape the unit_scalar class
--      uses), consumed by main.py's ASPECT-REL-AFFINITY ADMISSION (metadata-coupled: the
--      admission fires only when LIVE rows on the mapped rel exist, and only for
--      ENTITY-ANCHORED asks — no rows → no admission → the abstention stands honestly).
--      The seed is the ONE measured member's inflectional forms (run/runs/running); a
--      tenant grows its own aspect→rel bindings without code edits.
--
--   2. address aliases (PROD-BUG-4's serve half / the filed PROD-BUG-5 class): the address
--      correction landed (user, lives_at, "12 Bramble Court …") LIVE while "what's my home
--      address?" abstained honest-empty — no rel_type_aliases row names the address
--      surface, so ``lives_at`` never entered scalar_rels and the user-anchor D1 scalar
--      fetch never rode. The fix seeds the address surfaces onto the CANONICAL rel the
--      way migration 030 seeded lives→lives_in/lives_at: bare "address" (the keyword loop
--      resolves it) + the compound surfaces "home_address"/"street_address" (the
--      grown-place possessed-NP lane resolves + consumes the compound's words).
--      The compound QUALIFIER surfaces (email/ip/mac) are seeded onto THEIR canonical
--      scalar rels so a compound ask resolves and CONSUMES as one unit — without them,
--      seeding bare "address" would let "what's my email address?" admit lives_at beside
--      has_email (the sibling-serve collision the C5 bank-account probe measured). Every
--      alias insert is GUARDED on the canonical rel existing (the FK would abort the file
--      on a schema missing one; the guard makes the seed idempotent + partial-safe).
--
-- NO DDL CHANGE: linguistic_cues was created by migration 105 (public + per-tenant,
-- general-by-category) and rel_type_aliases by migration 030 (public seed source + the
-- provisioning seeder blanket-copies into every NEW tenant — the template comment at
-- user_schema.sql:504). This migration only seeds public (the seed source) and fans the
-- rows out to EXISTING tenant schemas (the 308/316 pattern). Idempotent: ON CONFLICT DO
-- NOTHING (linguistic_cues UNIQUE (cue, category); rel_type_aliases UNIQUE (alias)).
-- Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache
-- (GET /internal/refresh-intent-pattern-caches) or wait the 5s TTL.

-- Guard: create the table in public if migration 105 has not run (same idempotent DDL as 105/308/316).
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
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the aspect_rel_affinity class
-- ============================================================================
-- 'cue' is matched as a whole WORD in the question (word-boundary); 'description' carries
-- the canonical rel_type the aspect binds to. Membership drives a LIVE-ROW-COUPLED
-- admission (main.py) — the seed stays MINIMAL (the one measured member).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('run',     'aspect_rel_affinity', 'run_on', 'what does my homelab hypervisor run?', 'seed_prod_drive_20261112', 0.92),
  ('runs',    'aspect_rel_affinity', 'run_on', 'what runs on the homelab hypervisor?', 'seed_prod_drive_20261112', 0.92),
  ('running', 'aspect_rel_affinity', 'run_on', 'what is running on the homelab hypervisor?', 'seed_prod_drive_20261112', 0.92)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Seed public.rel_type_aliases (TEMPLATE / SEED-SOURCE ONLY) — the address
-- surfaces + the compound qualifiers. GUARDED inserts: each row lands only when its
-- canonical rel_type exists in public.rel_types (the FK would otherwise abort the file on
-- a schema that never grew that rel — partial-safe + idempotent by UNIQUE (alias)).
-- ============================================================================
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence)
SELECT v.canonical, v.alias, 'seed_prod_drive_20261112', 0.92
FROM (VALUES
    ('lives_at',   'address'),
    ('lives_at',   'home_address'),
    ('lives_at',   'street_address'),
    ('has_email',  'email_address'),
    ('has_ip',     'ip_address'),
    ('has_mac',    'mac_address')
) AS v(canonical, alias)
WHERE EXISTS (SELECT 1 FROM public.rel_types r WHERE r.rel_type = v.canonical)
ON CONFLICT (alias) DO NOTHING;

-- ============================================================================
-- Part 3: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- (the 308/316 fan-out; the guarded loop skips a partially-provisioned seat instead of
-- aborting the whole DO block)
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

        -- The alias fan-out mirrors the public guard: each row lands only when the
        -- tenant's own rel_types carries the canonical rel (rel_type_aliases is per-tenant
        -- and runtime-read UNQUALIFIED on the tenant search_path). GUARDED (the 313
        -- pattern): a partially-provisioned seat (no rel_type_aliases / no rel_types yet)
        -- is SKIPPED, never aborts the whole DO block.
        BEGIN
            EXECUTE format($alias$
                INSERT INTO %I.rel_type_aliases (canonical_rel_type, alias, source, confidence)
                SELECT v.canonical, v.alias, 'seed_prod_drive_20261112', 0.92
                FROM (VALUES
                    ('lives_at',   'address'),
                    ('lives_at',   'home_address'),
                    ('lives_at',   'street_address'),
                    ('has_email',  'email_address'),
                    ('has_ip',     'ip_address'),
                    ('has_mac',    'mac_address')
                ) AS v(canonical, alias)
                WHERE EXISTS (
                    SELECT 1 FROM %I.rel_types r WHERE r.rel_type = v.canonical)
                ON CONFLICT (alias) DO NOTHING
            $alias$, _schema, _schema);
        EXCEPTION
            WHEN undefined_table THEN NULL;  -- a schema with linguistic_cues but no rel_type_aliases yet
            WHEN OTHERS THEN NULL;
        END;

        RAISE NOTICE 'Migration 319: aspect_rel_affinity + address aliases seeded into %', _schema;
    END LOOP;
END $$;
