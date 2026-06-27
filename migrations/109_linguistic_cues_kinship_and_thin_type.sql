-- Migration 109: linguistic_cues — seed two MORE cue classes onto the SAME rail
-- Date: 2026-06-21
--
-- WHY
-- ---
-- Migration 105 created public.linguistic_cues as a GENERAL-by-`category` table (naming_verb); 108
-- added lvc_support_verb + svo_particle. Two FROZEN in-code lexical lists in
-- src/extraction/linguistics.py were the LAST spots breaking the "DB = single grown source of truth"
-- property (SPEC §2.1 / §5 gap-6) and are now converted to the identical DB-held + per-tenant +
-- overlay-resolved + growable contract:
--
--   * `_KINSHIP_RELATIONAL_NOUNS` (the kinship-vs-mereology split inside `_inherent_relation_for_noun`
--     in the genitive/relational-noun deriver: mother/father/sister/… → kinship → related_to; a
--     relational noun NOT in this class → component/part → part_of) → category 'kinship_noun'
--   * `_thin_type_for_token`'s inline `{system,device,gadget,appliance,machine} → device` MAP (the
--     coarse one-step "thin" slot-type tag; GLiNER2 supplies real typing and WINS over it) →
--     category 'thin_type'
--
-- Both in-code lists REMAIN as the DB-DOWN CODE-FALLBACK SEED only (linguistic_cue_overlay.
-- _BOOTSTRAP_KINSHIP_NOUNS / _BOOTSTRAP_THIN_TYPE_MAP), resolved via linguistic_cue_overlay.
-- resolve_kinship_nouns / resolve_thin_type — the SAME ContextVar-bound per-tenant overlay the
-- naming-verb / lvc_support / svo_particle / relational_noun / rel_type / taxonomy / temporal layers
-- use. The live authority is the DB; the in-code lists are the fail-safe (never weaker than today).
--
-- THE THIN-TYPE MAP ON A SET-SHAPED RAIL: linguistic_cues is keyed (cue, category). thin_type is a
-- MAP (surface → coarse type), not a set, so each mapping is ONE ROW: `cue` = the SURFACE head lemma,
-- `description` = the TARGET coarse type. resolve_thin_type() reads (cue, description) back into a
-- {surface: type} dict. No new table, no DDL change — the keyed value rides the existing rail.
--
-- WHAT STAYS IN CODE (genuinely closed — NOT data): the dependency RELATIONS (poss/case/appos/…) and
-- the universal POS function-word tag set. Only the NOUN-LEMMA recognition (kinship) and the
-- SURFACE→TYPE mapping (thin_type) become DB data that grows.
--
-- NO DDL CHANGE: 105 already created the table (public + per-tenant) general-by-category, and the
-- provisioning seeder (schema_manager.py) blanket-copies ALL public.linguistic_cues categories into
-- every NEW tenant — so new tenants inherit these classes automatically. This migration only
-- (1) seeds the two new categories into public and (2) fans them out to EXISTING tenant schemas.
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING. Safe to re-run.
-- NOTE: after applying, FLUSH the overlay cache (GET /internal/refresh-intent-pattern-caches) or
-- wait the 5s TTL.

-- Guard: if migration 105 has not run, create the table in public so this seed has a target. Same
-- DDL as 105 (idempotent).
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

-- (a) kinship_noun: the person↔person KINSHIP relational-noun class. `cue` is matched against the
--     spaCy noun lemma (lowercase). `description` carries the NOUN→REL_TYPE MAPPING the genitive /
--     possessive deriver reads (resolve_kinship_rel_map → {noun: rel_type}) so the inherent kin
--     relation is metadata-driven, NOT an in-code literal. The rel_type is the role the HEAD noun
--     plays toward the POSSESSOR ("my mother" → mother is the PARENT of me → parent_of; "my son" →
--     son is the CHILD of me → child_of; "my wife" → spouse). A kin with no exact 1-hop rel_type
--     (grandparent / uncle / aunt / cousin) maps to the generic ``related_to`` (the walk/ontology
--     grounds the specific kin downstream — we never fabricate a wrong direct rel). All string
--     literals are SINGLE-QUOTED with '' escaping (double quotes are SQL identifiers, not literals —
--     the prior double-quoted example_text values made this whole INSERT fail, leaving 0 rows).
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('mother',      'kinship_noun', 'parent_of',  'my mother''s birthday',  'seed_kinship', 0.95),
  ('father',      'kinship_noun', 'parent_of',  'my father''s car',       'seed_kinship', 0.95),
  ('mom',         'kinship_noun', 'parent_of',  'my mom''s phone',        'seed_kinship', 0.92),
  ('dad',         'kinship_noun', 'parent_of',  'my dad''s truck',        'seed_kinship', 0.92),
  ('parent',      'kinship_noun', 'parent_of',  'my parent''s house',     'seed_kinship', 0.90),
  ('sister',      'kinship_noun', 'sibling_of', 'my sister''s dog',       'seed_kinship', 0.93),
  ('brother',     'kinship_noun', 'sibling_of', 'my brother''s bike',     'seed_kinship', 0.93),
  ('sibling',     'kinship_noun', 'sibling_of', 'my sibling''s room',     'seed_kinship', 0.88),
  ('son',         'kinship_noun', 'child_of',   'my son''s school',       'seed_kinship', 0.90),
  ('daughter',    'kinship_noun', 'child_of',   'my daughter''s class',   'seed_kinship', 0.90),
  ('child',       'kinship_noun', 'child_of',   'my child''s teacher',    'seed_kinship', 0.88),
  ('wife',        'kinship_noun', 'spouse',     'my wife''s job',         'seed_kinship', 0.93),
  ('husband',     'kinship_noun', 'spouse',     'my husband''s car',      'seed_kinship', 0.93),
  ('spouse',      'kinship_noun', 'spouse',     'my spouse''s name',      'seed_kinship', 0.90),
  ('partner',     'kinship_noun', 'spouse',     'my partner''s family',   'seed_kinship', 0.85),
  ('uncle',       'kinship_noun', 'related_to', 'my uncle''s farm',       'seed_kinship', 0.88),
  ('aunt',        'kinship_noun', 'related_to', 'my aunt''s cabin',       'seed_kinship', 0.88),
  ('cousin',      'kinship_noun', 'related_to', 'my cousin''s wedding',   'seed_kinship', 0.85),
  ('grandmother', 'kinship_noun', 'related_to', 'my grandmother''s recipe', 'seed_kinship', 0.88),
  ('grandfather', 'kinship_noun', 'related_to', 'my grandfather''s watch',  'seed_kinship', 0.88),
  ('grandma',     'kinship_noun', 'related_to', 'my grandma''s house',    'seed_kinship', 0.85),
  ('grandpa',     'kinship_noun', 'related_to', 'my grandpa''s chair',    'seed_kinship', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- (b) thin_type: the surface→coarse-type MAP. `cue` = the SURFACE head lemma, `description` = the
--     TARGET coarse type (resolve_thin_type() reads them back as a {surface: type} dict). Verbatim
--     contents of the retired in-code _thin_type_for_token map. A slot tag only — GLiNER2 wins over it.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('system',    'thin_type', 'device', 'gps system', 'seed_thin_type', 0.80),
  ('device',    'thin_type', 'device', 'the device', 'seed_thin_type', 0.80),
  ('gadget',    'thin_type', 'device', 'a gadget',   'seed_thin_type', 0.78),
  ('appliance', 'thin_type', 'device', 'an appliance', 'seed_thin_type', 0.78),
  ('machine',   'thin_type', 'device', 'the machine', 'seed_thin_type', 0.78)
ON CONFLICT (cue, category) DO NOTHING;

-- (c) unit_scalar: the measurement-unit → SCALAR rel_type MAP for the copula measurement chain
--     ("she is 62 years old" → unit 'year' → age). `cue` = the unit head lemma, `description` = the
--     scalar rel_type (age/height/weight — all tail_types={SCALAR} so the value routes to
--     entity_attributes). resolve_unit_scalar_map() reads them as a {unit: rel_type} dict. A unit
--     OUTSIDE this map → no scalar minted (we never guess a measurement). Mirrors
--     _BOOTSTRAP_UNIT_SCALAR_MAP. All literals single-quoted.
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('year',       'unit_scalar', 'age',    'she is 62 years old', 'seed_unit_scalar', 0.92),
  ('foot',       'unit_scalar', 'height', 'he is 6 feet tall',   'seed_unit_scalar', 0.85),
  ('feet',       'unit_scalar', 'height', 'he is 6 feet tall',   'seed_unit_scalar', 0.85),
  ('inch',       'unit_scalar', 'height', 'she is 5 foot 4 inches', 'seed_unit_scalar', 0.82),
  ('centimetre', 'unit_scalar', 'height', '180 centimetres tall', 'seed_unit_scalar', 0.82),
  ('centimeter', 'unit_scalar', 'height', '180 centimeters tall', 'seed_unit_scalar', 0.82),
  ('cm',         'unit_scalar', 'height', '180 cm tall',         'seed_unit_scalar', 0.80),
  ('metre',      'unit_scalar', 'height', '1.8 metres tall',     'seed_unit_scalar', 0.80),
  ('meter',      'unit_scalar', 'height', '1.8 meters tall',     'seed_unit_scalar', 0.80),
  ('pound',      'unit_scalar', 'weight', 'he weighs 180 pounds', 'seed_unit_scalar', 0.85),
  ('lb',         'unit_scalar', 'weight', '180 lbs',             'seed_unit_scalar', 0.80),
  ('kilogram',   'unit_scalar', 'weight', '80 kilograms',        'seed_unit_scalar', 0.85),
  ('kg',         'unit_scalar', 'weight', '80 kg',               'seed_unit_scalar', 0.80),
  ('kilo',       'unit_scalar', 'weight', '80 kilos',            'seed_unit_scalar', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
-- The table already exists in each tenant (105 / user_schema.sql). Create it if missing (defensive,
-- same DDL), then seed the TWO NEW categories from public. Mirrors 105/108's fan-out. Idempotent.

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

        -- ---- seed the TWO NEW categories from public (template) ----
        EXECUTE format($seed$
            INSERT INTO %I.linguistic_cues
                (cue, category, frequency, confirmed_count, rejected_count,
                 correction_count, global_confidence, description, example_text,
                 source, is_active, archived_at, last_matched_at)
            SELECT cue, category, frequency, confirmed_count, rejected_count,
                   correction_count, global_confidence, description, example_text,
                   source, is_active, archived_at, last_matched_at
            FROM public.linguistic_cues
            WHERE category IN ('kinship_noun', 'thin_type', 'unit_scalar')
            ON CONFLICT (cue, category) DO NOTHING
        $seed$, _schema);

        RAISE NOTICE 'Migration 109: kinship_noun + thin_type cues seeded into %', _schema;
    END LOOP;
END $$;
