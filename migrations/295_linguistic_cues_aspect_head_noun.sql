-- Migration 290: linguistic_cues — seed the `aspect_head_noun` cue class (issue #226)
-- Date: 2026-10-07
--
-- WHY
-- ---
-- Cross-restatement entity convergence (issue #226, measured on prod 2026-10-07, fresh seat): the
-- owner said the same referent three ways in one session — "My opnsense box sits at…" / "It resides
-- on VLAN 30" / "My opnsense box resides on…" — and ingest minted THREE entities ('opnsense box',
-- 'firewall', 'opnsense box info'), each accruing its own facts. The 'opnsense box info' rival is
-- the ASPECT-HEAD shape: an NP whose head is an abstract NON-DENOTING noun of aspect — the info OF
-- the opnsense box, the details OF the kiln — denotes the SPECIFIC compound beside it, not a new
-- referent (CGEL ch.5 §8-§9: abstract nouns of property/aspect under right-headed composition).
--
-- The #226 convergence rule at the mint seam (EntityRegistry.resolve) therefore strips a TRAILING
-- aspect head before comparing the new surface against the recent-entity alias set:
--     'opnsense box info'  →  'opnsense box'  →  exact match  →  ONE referent.
-- The class lives on THIS (cue, category) rail — DB-held, per-tenant, GROWABLE, suppressions
-- honored — exactly like kinship_noun / dosage_noun (migrations 109/280), so the strip set is
-- never a code wordlist and a tenant may grow or deactivate members.
--
-- CLOSED-CLASS = English structure, SEED it (the migration-272 ruling that authorises seeding
-- shelf classes); the eight members are grammar-level abstract nouns recurring across every
-- domain (info / information / detail / details / status / summary / overview / description),
-- NOT subject matter. Deliberately EXCLUDED are aspect-adjacent heads that CAN denote standalone
-- referents ('note', 'notes', 'data', 'record' — "my study notes" is a thing; 'record' is already
-- the measure_noun canonical) — a wrong strip would merge two referents, THE HARD LINE, so the
-- seed admits only unambiguous non-denoting heads.
--
-- NO DDL CHANGE: 105 created the table (public + per-tenant) general-by-category and the
-- provisioning seeder blanket-copies ALL public.linguistic_cues categories into every NEW tenant —
-- new tenants inherit this class automatically. This migration only (1) seeds the new category
-- into public and (2) fans it out to EXISTING tenant schemas. Idempotent:
-- ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- ============================================================================
-- Part 1: Seed public (TEMPLATE / SEED-SOURCE ONLY) with the new class
-- ============================================================================

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('info',        'aspect_head_noun', 'non-denoting aspect head',
   'my opnsense box info is pinned above the desk', 'seed_aspect_head_noun', 0.95),
  ('information', 'aspect_head_noun', 'non-denoting aspect head',
   'the skutt kiln information sheet is in the folder', 'seed_aspect_head_noun', 0.95),
  ('detail',      'aspect_head_noun', 'non-denoting aspect head',
   'the payment detail is in my notes', 'seed_aspect_head_noun', 0.95),
  ('details',     'aspect_head_noun', 'non-denoting aspect head',
   'the skutt kiln details are in my notebook', 'seed_aspect_head_noun', 0.95),
  ('status',      'aspect_head_noun', 'non-denoting aspect head',
   'my opnsense box status is green', 'seed_aspect_head_noun', 0.95),
  ('summary',     'aspect_head_noun', 'non-denoting aspect head',
   'the meeting summary is on the whiteboard', 'seed_aspect_head_noun', 0.95),
  ('overview',    'aspect_head_noun', 'non-denoting aspect head',
   'the garden overview is taped inside the shed door', 'seed_aspect_head_noun', 0.95),
  ('description', 'aspect_head_noun', 'non-denoting aspect head',
   'the trail description is at the trailhead', 'seed_aspect_head_noun', 0.95)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (105 / user_schema.sql). Create it if missing (defensive,
-- same DDL), then seed the NEW category from public. Mirrors 279/280's fan-out. Idempotent.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- ---- table DDL (tenant-local, defensive) ----
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

        -- ---- seed the NEW category from public (template) ----
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category = 'aspect_head_noun'
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);
    END LOOP;
END $$;
