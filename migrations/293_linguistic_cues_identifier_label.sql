-- Migration 293: linguistic_cues — seed the identifier_noun 'label' role (issue #225)
-- Date: 2026-10-07
--
-- WHY
-- ---
-- Measured wound (prod, fresh seat): "My opnsense box sits at 192.168.0.1 on VLAN 30." →
-- (opnsense box, sit_at_on, vlan) — a verb-mash rel with the bare label noun minted as a
-- relational object entity — and the numeral 30 ANNIHILATED (residue-uncovered). Owner ruling
-- (2026-10-07): a label+code construction must GROW the ontology around the label and store the
-- value as a grounded memory hanging off that structure — smart on ingest, dumb on recall, for
-- ALL things.
--
-- THE 'label' ROLE (a THIRD role on the SAME identifier_noun class, alongside 'strong'/'suffix'):
-- the UNIT-DESIGNATOR head of a label+code NP ("VLAN 30", "port 24", "channel 6", "rack 4") — a
-- noun whose POST-NOMINAL numeral designates ONE instance of the noun's kind. Grounded:
--   • Grammar — a post-nominal numeral has UNIQUE-REFERENCE (identifying) force, unlike the
--     prenominal cardinal's quantifying reading ("four movies" ≠ "movie 4"): "Numerals and their
--     Position in Universal Grammar", J. Universal Linguistics (Sejong) 6(2), §3 "Post-nominal
--     Numerals and Unique Reference", https://www.sejongjul.org/download/download_pdf?pid=jul-6-2-1
--   • Applied standard — the label-noun class is the USPS's "secondary address unit designator"
--     (APARTMENT/SUITE/UNIT/ROOM + identifying number; USPS Publication 28 §213,
--     https://pe.usps.com/text/pub28/28c2_003.htm).
-- Read by the spine's _chain_identifier_np (src/extraction/linguistics.py): "… on VLAN 30" →
-- (opnsense box, vlan, "30", scalar_datatype='string') — the CODE lands as a SCALAR ATTRIBUTE
-- NAMED BY THE NOUN on the clause subject, and the noun NEVER becomes a relational object (the
-- label token is claimed + the SVO twin suppressed; the noun's ontology growth rides the rel rail:
-- the attribute rel mints pending_placement and grooms — a rel_type surface can never be an
-- entity, which is exactly why no rival node can exist).
--
-- DELIBERATELY MINIMAL SEED (the measured prod wound's starters): vlan/port/channel/rack. Every
-- other domain's designators (a pottery tenant's 'cone', a sailing tenant's 'berth', a brewery's
-- 'vessel'/'batch') GROW per-tenant on this rail — the deriver queues a non-class noun in the
-- same structural shape (post-nominal digit-bearing nummod) as an identifier_noun cue candidate
-- (the V4a growth_out → record_cue_candidate → re_embedder freq≥3 rail), never a code edit.
--
-- Mirrors the DB-DOWN code-fallback in linguistic_cue_overlay._BOOTSTRAP_IDENTIFIER_NOUN_ROLE_MAP
-- (keep them row-for-row). NO DDL change: migration 105 created public.linguistic_cues
-- (+ per-tenant), and the provisioning seeder (schema_manager.py) blanket-copies ALL
-- public.linguistic_cues categories into every NEW tenant — so new tenants inherit the rows
-- automatically. This migration only (1) seeds public and (2) fans out to EXISTING tenant
-- schemas. Idempotent: ON CONFLICT (cue, category) DO NOTHING.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

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

-- ── Part 1: seed the public template ──────────────────────────────────────────
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('vlan',     'identifier_noun', 'label', 'my opnsense box sits at 192.168.0.1 on VLAN 30', 'seed_identifier_label', 0.90),
  ('port',     'identifier_noun', 'label', 'my switch uplinks on port 24',                   'seed_identifier_label', 0.88),
  ('channel',  'identifier_noun', 'label', 'the controller binds to channel 6',              'seed_identifier_label', 0.86),
  ('rack',     'identifier_noun', 'label', 'the kiln sits on rack 4',                        'seed_identifier_label', 0.86)
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
              ('vlan',     'identifier_noun', 'label', 'my opnsense box sits at 192.168.0.1 on VLAN 30', 'seed_identifier_label', 0.90),
              ('port',     'identifier_noun', 'label', 'my switch uplinks on port 24',                   'seed_identifier_label', 0.88),
              ('channel',  'identifier_noun', 'label', 'the controller binds to channel 6',              'seed_identifier_label', 0.86),
              ('rack',     'identifier_noun', 'label', 'the kiln sits on rack 4',                        'seed_identifier_label', 0.86)
            ON CONFLICT (cue, category) DO NOTHING
        $f$, _schema);
    END LOOP;
END $$;
