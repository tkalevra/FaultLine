-- 323_sense_layer_seed.sql — THE SENSE LAYER (DEV/research/LANGUAGE-STANDARDS-SPEC.md §2,
-- the OntoLex-inspired relational subset; spec checklist §10 items 1-2; BUG-RECALL-15's
-- storage half).
--
-- WHAT
-- ----
-- Three ADDITIVE lexicon tables + ONE additive foreign-key column on the lexical-entry
-- analog, in EVERY schema that holds a rel_types table (public + faultline_% + pin/seat):
--
--   senses                  — the sense layer. One row per (lemma, sense_class): the
--                             citable sense a lexical entry selects. The relational
--                             subset of OntoLex-Lemon's LexicalEntry—Sense relation,
--                             NOT a triple store (§2's explicit ruling).
--   verbnet_classes         — the seeded, citable VerbNet class inventory (the curated
--                             subset, §2/§6.2). Class ids + member lists verified
--                             verbatim against the VerbNet v3.2 online index
--                             (verbs.colorado.edu/verb-index, fetched 2026-11-13).
--   sense_verbnet_bindings  — the SENSE-SCOPED class membership: the binding records
--                             WHICH CLASS WHICH SENSE SELECTS — the primary key IS the
--                             §2 shape (sense_id, verbnet_class_id) — never a bare
--                             class on the verb.
--   rel_types.sense_id      — the lexical-entry foreign-key wiring (§2's
--                             "lexical_entries.sense_id"). The WGM rel_type rows ARE
--                             the lexical entries (the OntoLex mapping per the spec's
--                             implementation brief); sense_id binds a rel row to the
--                             sense it was minted under. NULL = sense-unbound (every
--                             existing row; the default).
--
-- WHY
-- ---
-- BUG-RECALL-15's storage half: the recall matched the RAW TOKEN "family" across the
-- store — the kinship sense (the user's family), the taxonomy sense ("language
-- family"), and the medical-history sense ("a family history of diabetes") all match,
-- and token-frequency ranking served the reference-corpus documents over the user's
-- family rows. The sense layer gives the polysemous lemma citable sense rows; the
-- recall-side disambiguation that READS them ships in the same increment
-- (RECALL_SENSE_DISAMBIGUATION, src/api/main.py + src/mcp/server.py) — §8.1: two
-- halves of one fix, shipped together. The seed set is the EXAMPLE/VERIFICATION set
-- from the filings, NOT the full inventory: the full senses grow through the WGM like
-- the rel_types (the WGM gate is never bypassed — nothing in this migration or in the
-- runtime reads writes to senses; growth rows carry source='wgm' when the WGM lane
-- lands).
--
-- THE SEED (the example/verification set, from the filings)
-- ---------------------------------------------------------
-- senses:
--   family#kinship         — the kinship-group reading (BUG-RECALL-15 sense (a));
--                            the lemma's DEFAULT (the possessive-kinship cue selects it)
--   family#taxonomy        — the classification reading (sense (b), "language
--                            family"); discriminators = the compound co-nouns the
--                            filing's detector keys on
--   family#medical_history — the medical-history collocation reading (sense (c),
--                            "family history of <condition>"); discriminator 'history'
--   use#instrumentality    — the VerbNet-class reading of "use" (the VerbNet scoping:
--                            the verb class binds exactly this sense;
--                            standard_ref verbnet:use-105, members verified)
--   use#consumption        — the use-up/expend verb reading (the scoping demonstrator:
--                            a verb sense the VerbNet class does NOT cover)
--   use#utility            — the noun reading ("the use of X"; VerbNet binds verb
--                            classes — the noun readings sit outside the scoping)
-- verbnet_classes: 14 core classes for personal state tracking (§10.4's domains:
-- possession, transfer, desire, preference, speech, message, placement, sending,
-- existence, holding, storage, meeting, consumption, instrumentality). Every class_id
-- and every member verb was verified verbatim against the online index before seeding.
-- sense_verbnet_bindings: exactly ONE seed row — use#instrumentality → use-105 — the
-- §2 shape made concrete ("which class which sense selects"): the SAME lemma's
-- consumption sense binds NOTHING.
--
-- TEMPLATE + MIGRATION, NEVER ONE WITHOUT THE OTHER (the 197/273/298/299/300/301/317
-- rule): src/provisioning/templates/user_schema.sql gains the same three tables and
-- the same rel_types.sense_id column (appended LAST to match this file's ALTER
-- ordinal, so the bootstrap `INSERT INTO rel_types SELECT * FROM public.rel_types`
-- stays column-aligned) in the SAME commit, and src/provisioning/schema_manager.py
-- gains the fresh-mint seed (public = the SEED SOURCE, the rel_type_aliases pattern).
--
-- FAN-OUT: every schema holding a rel_types table (the 308/309/313/314/317 probe
-- pattern — pg_namespace EXISTS on the table name; the information_schema LIKE loop
-- misses non-faultline_ prefixed schemas), guarded per-schema: one bad or partially-
-- provisioned schema skips itself instead of aborting the DO block (the b99ce200
-- discipline). ADDITIVE + IDEMPOTENT only: CREATE TABLE IF NOT EXISTS, ADD COLUMN IF
-- NOT EXISTS, guarded constraint adds (duplicate_object → NULL), ON CONFLICT DO
-- NOTHING seeds. NO backfill, NO update of any existing row, NO delete. Safe to re-run.
--
-- IDENTITY SEMANTICS: the serial ids are PER-SCHEMA (a tenant's senses.id differs from
-- public's); every cross-schema copy maps through the NATURAL keys (sense_key /
-- class_id) and every runtime read goes through the tenant table UNQUALIFIED on the
-- tenant search_path (the rel_type_aliases discipline — public is the SEED SOURCE
-- only, never read at runtime).

-- ── Part 1: the public SEED SOURCE tables (public only; tenants copy from here) ─────
CREATE TABLE IF NOT EXISTS public.senses (
    id BIGSERIAL PRIMARY KEY,
    -- The citable sense id ('family#kinship') — the OntoLex sense ref the receipt
    -- (§3) and the recall projection carry. UNIQUE: one row per sense.
    sense_key TEXT NOT NULL UNIQUE,
    -- The lexical entry's lemma ('family', 'use') — the token the sense lookup keys on.
    lemma TEXT NOT NULL,
    -- The UD/Universal POS of the sense-bearing token. NULL = unspecified.
    pos TEXT,
    -- The coarse sense class — the discriminable reading (the CHECK domain below is
    -- the closed seed set + 'other' for WGM-grown rows; later migrations widen it the
    -- way chk_rel_types_scalar_datatype was widened).
    sense_class TEXT NOT NULL,
    -- The gloss, in our words (never shipped verbatim as memory — provenance text).
    definition TEXT,
    -- The citable standard reference ('verbnet:use-105', 'framenet:Kinship',
    -- 'wn:family'). NULL = our own filing-backed class with no external standard.
    standard_ref TEXT,
    -- The compound co-noun discriminators: when the lemma token stands in a compound
    -- collocation with one of these co-nouns ("language" + "family" → family#taxonomy,
    -- "history" + "family" → family#medical_history), the UD anchor selects THAT sense.
    discriminators TEXT[] NOT NULL DEFAULT '{}',
    -- The lemma's default sense when no anchor decides (one per lemma).
    default_sense BOOLEAN NOT NULL DEFAULT false,
    -- Provenance: 'seed' (this migration) now; 'wgm' when the WGM lane grows senses;
    -- 'user' for operator-minted rows. The WGM gate is never bypassed.
    source TEXT NOT NULL DEFAULT 'seed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT senses_pos_domain CHECK (pos IS NULL OR pos IN ('NOUN', 'VERB', 'ADJ', 'ADV')),
    CONSTRAINT senses_sense_class_domain CHECK (sense_class IN (
        'kinship', 'taxonomy', 'medical_history', 'instrumentality',
        'consumption', 'utility', 'other')),
    CONSTRAINT senses_source_domain CHECK (source IN ('seed', 'wgm', 'user')),
    CONSTRAINT senses_one_per_lemma_class UNIQUE (lemma, pos, sense_class)
);
CREATE INDEX IF NOT EXISTS idx_senses_lemma ON public.senses (lemma);

CREATE TABLE IF NOT EXISTS public.verbnet_classes (
    id BIGSERIAL PRIMARY KEY,
    -- The VerbNet class identifier ('use-105') — the citable inventory key (§2).
    class_id TEXT NOT NULL UNIQUE,
    -- Our short descriptor (not a VerbNet field — labeling provenance is ours).
    label TEXT NOT NULL,
    -- The verified member verbs (verbatim from the online index).
    members TEXT[] NOT NULL DEFAULT '{}',
    -- The lexicon version the class ids + members were verified against.
    lexicon_ref TEXT NOT NULL DEFAULT 'verbnet:3.2',
    source TEXT NOT NULL DEFAULT 'seed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT verbnet_classes_source_domain CHECK (source IN ('seed', 'wgm', 'user'))
);

-- THE §2 SHAPE: class membership is sense-scoped — the primary key IS
-- (sense_id, verbnet_class_id). "Which class which sense selects", never a bare
-- class on the verb.
CREATE TABLE IF NOT EXISTS public.sense_verbnet_bindings (
    sense_id BIGINT NOT NULL REFERENCES public.senses(id) ON DELETE CASCADE,
    verbnet_class_id BIGINT NOT NULL REFERENCES public.verbnet_classes(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'seed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sense_id, verbnet_class_id),
    CONSTRAINT sense_verbnet_bindings_source_domain CHECK (source IN ('seed', 'wgm', 'user'))
);

-- ── Part 2: the public seed rows (the example/verification set) ──────────────────────
INSERT INTO public.senses
    (sense_key, lemma, pos, sense_class, definition, standard_ref,
     discriminators, default_sense, source)
VALUES
  ('family#kinship', 'family', 'NOUN', 'kinship',
   'the kinship-group reading — the speaker''s family members (spouse, kids, pets); '
   'the possessive-kinship cue selects this sense',
   'framenet:Kinship', '{}', true, 'seed'),
  ('family#taxonomy', 'family', 'NOUN', 'taxonomy',
   'the classification reading — a set of things sharing an attribute '
   '("language family", taxonomic family)',
   'wn:family', '{language,linguistic}', false, 'seed'),
  ('family#medical_history', 'family', 'NOUN', 'medical_history',
   'the medical-history collocation reading — "a family history of <condition>"',
   NULL, '{history}', false, 'seed'),
  ('use#instrumentality', 'use', 'VERB', 'instrumentality',
   'the VerbNet-class reading — put into service, employ (apply/employ/exert/'
   'exploit/reuse/use/utilize)',
   'verbnet:use-105', '{}', true, 'seed'),
  ('use#consumption', 'use', 'VERB', 'consumption',
   'the use-up/expend reading — a verb sense the use-105 class does NOT cover '
   '(the VerbNet scoping demonstrator)',
   'wn:use', '{}', false, 'seed'),
  ('use#utility', 'use', 'NOUN', 'utility',
   'the noun reading — usefulness / an act of use; VerbNet binds verb classes, '
   'the noun readings sit outside the scoping',
   'wn:use', '{}', false, 'seed')
ON CONFLICT (sense_key) DO NOTHING;

-- The 14 core VerbNet classes (§10.4's personal-state domains). Every class_id and
-- every member below was fetched and verified verbatim against the VerbNet v3.2
-- online index (verbs.colorado.edu/verb-index) on 2026-11-13 — no member is guessed.
INSERT INTO public.verbnet_classes
    (class_id, label, members, lexicon_ref, source)
VALUES
  ('own-100', 'ownership & possession',
   '{have,own,possess}', 'verbnet:3.2', 'seed'),
  ('give-13.1', 'transfer of possession',
   '{deal,lend,loan,pass,peddle,refund,render}', 'verbnet:3.2', 'seed'),
  ('want-32.1', 'desire',
   '{covet,crave,desire,fancy}', 'verbnet:3.2', 'seed'),
  ('admire-31.2', 'preference & affect (experiencer—stimulus)',
   '{abhor,admire,adore,affirm,applaud,appreciate,bear,believe,bewail,cherish,deify,'
   'deplore,detest,disbelieve,distrust,dread,envy,esteem,exalt,execrate,fancy,favor,'
   'grudge,idolize,lament,loathe,miss,mistrust,mourn,pity,prefer,prize,reaffirm,'
   'relish,resent,respect,revere,rue,savor,stand,suffer,support,tolerate,treasure,'
   'trust,value,venerate,worship}', 'verbnet:3.2', 'seed'),
  ('say-37.7', 'speech act',
   '{add,allege,blabber,disclose,divulge,exclaim,insinuate,insist,intimate,leak,'
   'promulgate,reply,respond,retort,utter,venture,vocalize,voice,volunteer,write}',
   'verbnet:3.2', 'seed'),
  ('tell-37.2', 'informative message transfer',
   '{acquaint,advise,apprise,inform,notify,remind,update}', 'verbnet:3.2', 'seed'),
  ('put-9.1', 'placement',
   '{arrange,emplace,immerse,implant,lodge,mount,position,situate,sling,station,'
   'superimpose,tack_on,wharf}', 'verbnet:3.2', 'seed'),
  ('send-11.1', 'caused motion (sending)',
   '{convey,deliver,dispatch,export,handdeliver,import,pass_on,port,post,return,'
   'shift,shunt,transfer,transport,transship}', 'verbnet:3.2', 'seed'),
  ('exist-47.1', 'existence',
   '{overspread}', 'verbnet:3.2', 'seed'),
  ('hold-15.1', 'holding & contact',
   '{clasp,clutch,grab,grasp,grip,handle,hold,pull,push,seize,tug,wield,yank}',
   'verbnet:3.2', 'seed'),
  ('keep-15.2', 'keeping & storage',
   '{hoard,keep,leave,stock,stockpile,store}', 'verbnet:3.2', 'seed'),
  ('meet-36.3', 'social meeting',
   '{consult,deliberate,meet,play,remonstrate,reunite,visit}', 'verbnet:3.2', 'seed'),
  ('consume-66', 'consumption',
   '{consume,pass,use}', 'verbnet:3.2', 'seed'),
  ('use-105', 'instrumentality use',
   '{apply,employ,exert,exploit,reuse,use,utilize}', 'verbnet:3.2', 'seed')
ON CONFLICT (class_id) DO NOTHING;

-- THE ONE SENSE-SCOPED BINDING: use#instrumentality → use-105. The §2 shape made
-- concrete — the consumption sense of the SAME lemma binds NOTHING (the scoping).
INSERT INTO public.sense_verbnet_bindings
    (sense_id, verbnet_class_id, source)
SELECT s.id, v.id, 'seed'
FROM public.senses s
JOIN public.verbnet_classes v ON v.class_id = 'use-105'
WHERE s.sense_key = 'use#instrumentality'
ON CONFLICT (sense_id, verbnet_class_id) DO NOTHING;

-- ── Part 3: the fan-out — every schema holding a rel_types table ────────────────────
DO $mig$
DECLARE
    _sch text;
BEGIN
    FOR _sch IN
        SELECT n.nspname
          FROM pg_namespace n
         WHERE EXISTS (
               SELECT 1
                 FROM pg_class c
                WHERE c.relnamespace = n.oid
                  AND c.relname = 'rel_types'
                  AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        BEGIN
            -- The sense layer, schema-local (ids are per-schema; seeds copy through the
            -- natural keys, never the serials).
            EXECUTE format('CREATE TABLE IF NOT EXISTS %I.senses (
                id BIGSERIAL PRIMARY KEY,
                sense_key TEXT NOT NULL UNIQUE,
                lemma TEXT NOT NULL,
                pos TEXT,
                sense_class TEXT NOT NULL,
                definition TEXT,
                standard_ref TEXT,
                discriminators TEXT[] NOT NULL DEFAULT ''{}'',
                default_sense BOOLEAN NOT NULL DEFAULT false,
                source TEXT NOT NULL DEFAULT ''seed'',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT senses_pos_domain CHECK (pos IS NULL OR pos IN (''NOUN'', ''VERB'', ''ADJ'', ''ADV'')),
                CONSTRAINT senses_sense_class_domain CHECK (sense_class IN (
                    ''kinship'', ''taxonomy'', ''medical_history'', ''instrumentality'',
                    ''consumption'', ''utility'', ''other'')),
                CONSTRAINT senses_source_domain CHECK (source IN (''seed'', ''wgm'', ''user'')),
                CONSTRAINT senses_one_per_lemma_class UNIQUE (lemma, pos, sense_class)
            )', _sch);
            EXECUTE format('CREATE INDEX IF NOT EXISTS idx_senses_lemma ON %I.senses (lemma)', _sch);
            EXECUTE format('CREATE TABLE IF NOT EXISTS %I.verbnet_classes (
                id BIGSERIAL PRIMARY KEY,
                class_id TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                members TEXT[] NOT NULL DEFAULT ''{}'',
                lexicon_ref TEXT NOT NULL DEFAULT ''verbnet:3.2'',
                source TEXT NOT NULL DEFAULT ''seed'',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT verbnet_classes_source_domain CHECK (source IN (''seed'', ''wgm'', ''user''))
            )', _sch);
            EXECUTE format('CREATE TABLE IF NOT EXISTS %I.sense_verbnet_bindings (
                sense_id BIGINT NOT NULL REFERENCES %I.senses(id) ON DELETE CASCADE,
                verbnet_class_id BIGINT NOT NULL REFERENCES %I.verbnet_classes(id) ON DELETE CASCADE,
                source TEXT NOT NULL DEFAULT ''seed'',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (sense_id, verbnet_class_id),
                CONSTRAINT sense_verbnet_bindings_source_domain CHECK (source IN (''seed'', ''wgm'', ''user''))
            )', _sch, _sch, _sch);

            -- The seeds: copy the public template rows through the NATURAL keys.
            EXECUTE format($seed$
                INSERT INTO %I.senses
                    (sense_key, lemma, pos, sense_class, definition, standard_ref,
                     discriminators, default_sense, source)
                SELECT sense_key, lemma, pos, sense_class, definition, standard_ref,
                       discriminators, default_sense, source
                FROM public.senses
                WHERE source = 'seed'
                ON CONFLICT (sense_key) DO NOTHING
            $seed$, _sch);
            EXECUTE format($seed$
                INSERT INTO %I.verbnet_classes
                    (class_id, label, members, lexicon_ref, source)
                SELECT class_id, label, members, lexicon_ref, source
                FROM public.verbnet_classes
                WHERE source = 'seed'
                ON CONFLICT (class_id) DO NOTHING
            $seed$, _sch);
            EXECUTE format($seed$
                INSERT INTO %I.sense_verbnet_bindings
                    (sense_id, verbnet_class_id, source)
                SELECT ls.id, lv.id, b.source
                FROM public.sense_verbnet_bindings b
                JOIN public.senses s ON s.id = b.sense_id
                JOIN public.verbnet_classes v ON v.id = b.verbnet_class_id
                JOIN %I.senses ls ON ls.sense_key = s.sense_key
                JOIN %I.verbnet_classes lv ON lv.class_id = v.class_id
                ON CONFLICT (sense_id, verbnet_class_id) DO NOTHING
            $seed$, _sch, _sch, _sch);

            -- THE LEXICAL-ENTRY FK WIRING (§2's lexical_entries.sense_id): the column
            -- appended LAST (matching the template ordinal), then the guarded FK.
            EXECUTE format('ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS sense_id BIGINT', _sch);
            EXECUTE format('ALTER TABLE %I.rel_types ADD CONSTRAINT rel_types_sense_id_fk '
                           'FOREIGN KEY (sense_id) REFERENCES %I.senses(id) ON DELETE SET NULL',
                           _sch, _sch);
        EXCEPTION
            WHEN duplicate_object THEN NULL;  -- the FK already exists (idempotent re-run)
            WHEN undefined_table THEN NULL;   -- rel_types dropped between probe and ALTER
            WHEN OTHERS THEN
                -- one bad schema never skips the rest (the b99ce200 discipline) — but a
                -- swallowed error must be GREPPABLE: the 2026-11-13 boot shipped a
                -- format() arg-count bug that failed EVERY tenant schema silently
                -- (three %I, two _sch in the bindings seed) and the bare NULL hid it
                -- until the post-boot fan audit caught sense_id at 1-of-147. Any OTHER
                -- warns loud with the schema + the sqlstate/errm.
                RAISE WARNING '323fan schema=% sqlstate=% sqlerrm=%', _sch, SQLSTATE, SQLERRM;
        END;
    END LOOP;
END
$mig$;
