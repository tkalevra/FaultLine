-- Migration 282: Italian (it) seeds for the cue classes the Italian-aware engine chains read.
--
-- Rule (same rail as every cue migration): the CODE carries no Italian word list; the Italian members
-- of each closed lexical class live in the DATABASE (public template -> copied into every tenant
-- schema, grown per tenant). Every row below is a class membership grounded in Italian grammar, and
-- every class seeded here has a LIVE consumer on the Italian parse — a row nothing reads would be a
-- dead cue that overclaims capture.
--
--   • naming_verb — the reflexive naming verb "chiamarsi". Read by _chain_named_role (and the
--     possessive chain's naming interplay guard) on the UD PRONOMINAL frame only: the verb names
--     when a reflexive clitic fills its object slot ("mia figlia si chiama Anna", "mi chiamo
--     Marco"); the clitic-less active "chiama Anna" (= calls Anna) is never a naming frame
--     (linguistics._naming_frame_licensed). 'chiamo' is the 1sg surface: it_core_news_sm lemmatizes
--     it to the non-word 'chire'/'chare' (measured), and the UD member test reads lemma OR surface.
--   • first_person_possessive (NEW class) — the first-person possessive determiners. UD puts person
--     on the possessive (Person=1 with Poss=Yes); it_core_news_sm drops it on mio/mia/miei/mie/
--     nostro/… (measured: Poss=Yes|PronType=Prs only), so the speaker's "mia figlia" read as a third
--     party. The pipeline bridge (linguistics._ud_first_person_possessive_repair) restores Person=1
--     on a Poss=Yes token whose lemma or surface is a member. Lemmas mio/nostro plus every inflected
--     surface (the lemmatizer is not trusted to fold them; sentence-initial "Mia" keeps its
--     capital as lemma).
--   • kinship_noun — relational kin nouns -> their inherent relation (description), read by the
--     naming chain ("mia figlia si chiama Anna" -> (anna, child_of, user)) and the possessive
--     person-role ladder ("mia sorella" -> (sorella, sibling_of, user)). Masculine, feminine and
--     plural surfaces are all seeded: it_core_news_sm folds some forms (figlie -> figlio,
--     nonna -> nonno) and not others (figlia -> figlia).
--   • social_role — person-to-person ties, read by the possessive person-role ladder. Without them
--     "il mio amico" files a PERSON as an owned object (user, owns, amico). amico/amica -> friend_of
--     (the tie English 'friend' seeds); collega, vicino/vicina, coinquilino/coinquilina,
--     conoscente -> knows. 'compagno'/'compagna' is deliberately NOT seeded: it means both partner
--     (spouse) and companion/classmate, and a wrong tie is worse than none.
--   • dosage_noun / measure_noun — the measurement-family head nouns, read language-neutrally by the
--     registry's bare family-noun refusal (a bare "dose"/"livello" is an ATTRIBUTE, never an entity)
--     and the mint suppression. Italian: dose/dosi, dosaggio, quantità, livello; record, vocabolario.
--   • dimension_adjective / dimension_verb (NEW keyed classes) — row = "<dimension><-<unit rels>":
--     the adjective/verb NAMES the dimension, and the row applies only when the unit's own
--     unit_scalar rel is accepted ("dura 3 anni" → duration; "è lungo 2 ore" → nothing).
--   • unit_scalar / possession_verb / measure_verb / dimension_adjective — read by the
--     UD measure chain (linguistics "UD MEASURE PRE-PASS"): "mia sorella ha 30 anni" (possession
--     verb avere + unit anni -> age), "la corda è lunga 4 metri" (copular gradable adjective:
--     lungo -> length; the unit alone cannot tell length from height), "il film dura 2 ore" /
--     "mio padre pesa 80 chili" (measure verb + unit -> the unit map's rel). Time units (anno,
--     mese, settimana, giorno, ora, minuto, secondo) and metric units, singular and plural (the
--     lemmatizer is not trusted to fold them).
--   • rel_types length / width / depth — the SCALAR dimensions the dimension adjectives name,
--     modelled on the seeded 'height' row but with head_types {ANY} (a rope has a length). Only
--     inserted when absent; Italian natural_language templates (the query walk matches content
--     words of the query against this column).
--
-- NOT seeded, deliberately (see LEGGIMI-it.md): kinship_gender (the lemmatizer folds nonna -> nonno
-- and figlie -> figlio, so a lemma-keyed gender map would mis-gender; grammatical Gender is not
-- natural gender for every noun), naming_noun (the copular "il mio nome è X" chains read the English
-- be-AUX shape and never fire on the UD parse), relocation/natal/locative/implicative/… classes
-- (their chains read Penn prep->pobj/auxpass structure).
--
-- Idempotent: ON CONFLICT (cue, category) DO NOTHING.

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('chiamare',  'naming_verb', 'Reflexive naming verb: "<X> si chiama <Nome>".',          'Mia figlia si chiama Anna.', 'seed_naming_verb_it', 0.95),
  ('chiamo',    'naming_verb', '1sg surface of chiamarsi (lemmatizer miss): "Mi chiamo <Nome>".', 'Mi chiamo Marco.',      'seed_naming_verb_it', 0.90)
ON CONFLICT (cue, category) DO NOTHING;

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('mio',     'first_person_possessive', 'Person=1 possessive determiner (lemma, sing. possessor)', 'il mio cane',     'seed_first_person_possessive_it', 0.95),
  ('mia',     'first_person_possessive', 'Person=1 possessive determiner (fem. sing.)',            'mia figlia',      'seed_first_person_possessive_it', 0.95),
  ('miei',    'first_person_possessive', 'Person=1 possessive determiner (masc. plur.)',           'i miei figli',    'seed_first_person_possessive_it', 0.95),
  ('mie',     'first_person_possessive', 'Person=1 possessive determiner (fem. plur.)',            'le mie sorelle',  'seed_first_person_possessive_it', 0.95),
  ('nostro',  'first_person_possessive', 'Person=1 possessive determiner (lemma, plur. possessor)', 'il nostro gatto', 'seed_first_person_possessive_it', 0.95),
  ('nostra',  'first_person_possessive', 'Person=1 possessive determiner (fem. sing.)',            'la nostra casa',  'seed_first_person_possessive_it', 0.95),
  ('nostri',  'first_person_possessive', 'Person=1 possessive determiner (masc. plur.)',           'i nostri figli',  'seed_first_person_possessive_it', 0.95),
  ('nostre',  'first_person_possessive', 'Person=1 possessive determiner (fem. plur.)',            'le nostre figlie','seed_first_person_possessive_it', 0.95)
ON CONFLICT (cue, category) DO NOTHING;

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('madre',     'kinship_noun', 'parent_of',  'mia madre',       'seed_kinship_it', 0.95),
  ('padre',     'kinship_noun', 'parent_of',  'mio padre',       'seed_kinship_it', 0.95),
  ('mamma',     'kinship_noun', 'parent_of',  'mia mamma',       'seed_kinship_it', 0.92),
  ('papà',      'kinship_noun', 'parent_of',  'mio papà',        'seed_kinship_it', 0.92),
  ('babbo',     'kinship_noun', 'parent_of',  'mio babbo',       'seed_kinship_it', 0.90),
  ('genitore',  'kinship_noun', 'parent_of',  'un genitore',     'seed_kinship_it', 0.92),
  ('genitori',  'kinship_noun', 'parent_of',  'i miei genitori', 'seed_kinship_it', 0.92),
  ('figlio',    'kinship_noun', 'child_of',   'mio figlio',      'seed_kinship_it', 0.95),
  ('figlia',    'kinship_noun', 'child_of',   'mia figlia',      'seed_kinship_it', 0.95),
  ('figli',     'kinship_noun', 'child_of',   'i miei figli',    'seed_kinship_it', 0.92),
  ('figlie',    'kinship_noun', 'child_of',   'le mie figlie',   'seed_kinship_it', 0.92),
  ('fratello',  'kinship_noun', 'sibling_of', 'mio fratello',    'seed_kinship_it', 0.95),
  ('sorella',   'kinship_noun', 'sibling_of', 'mia sorella',     'seed_kinship_it', 0.95),
  ('fratelli',  'kinship_noun', 'sibling_of', 'i miei fratelli', 'seed_kinship_it', 0.92),
  ('sorelle',   'kinship_noun', 'sibling_of', 'le mie sorelle',  'seed_kinship_it', 0.92),
  ('marito',    'kinship_noun', 'spouse',     'mio marito',      'seed_kinship_it', 0.95),
  ('moglie',    'kinship_noun', 'spouse',     'mia moglie',      'seed_kinship_it', 0.95),
  ('coniuge',   'kinship_noun', 'spouse',     'il mio coniuge',  'seed_kinship_it', 0.90),
  ('nonno',     'kinship_noun', 'related_to', 'mio nonno',       'seed_kinship_it', 0.90),
  ('nonna',     'kinship_noun', 'related_to', 'mia nonna',       'seed_kinship_it', 0.90),
  ('nonni',     'kinship_noun', 'related_to', 'i miei nonni',    'seed_kinship_it', 0.88),
  ('zio',       'kinship_noun', 'related_to', 'mio zio',         'seed_kinship_it', 0.90),
  ('zia',       'kinship_noun', 'related_to', 'mia zia',         'seed_kinship_it', 0.90),
  ('zii',       'kinship_noun', 'related_to', 'i miei zii',      'seed_kinship_it', 0.88),
  ('cugino',    'kinship_noun', 'related_to', 'mio cugino',      'seed_kinship_it', 0.90),
  ('cugina',    'kinship_noun', 'related_to', 'mia cugina',      'seed_kinship_it', 0.90),
  ('cugini',    'kinship_noun', 'related_to', 'i miei cugini',   'seed_kinship_it', 0.88),
  ('nipote',    'kinship_noun', 'related_to', 'mio nipote',      'seed_kinship_it', 0.88),
  ('nipoti',    'kinship_noun', 'related_to', 'i miei nipoti',   'seed_kinship_it', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('amico',       'social_role', 'friend_of', 'il mio amico Luca',   'seed_social_role_it', 0.90),
  ('amica',       'social_role', 'friend_of', 'la mia amica Giulia', 'seed_social_role_it', 0.90),
  ('amici',       'social_role', 'friend_of', 'i miei amici',        'seed_social_role_it', 0.85),
  ('amiche',      'social_role', 'friend_of', 'le mie amiche',       'seed_social_role_it', 0.85),
  ('collega',     'social_role', 'knows',     'la mia collega Sara', 'seed_social_role_it', 0.85),
  ('colleghi',    'social_role', 'knows',     'i miei colleghi',     'seed_social_role_it', 0.82),
  ('colleghe',    'social_role', 'knows',     'le mie colleghe',     'seed_social_role_it', 0.82),
  ('vicino',      'social_role', 'knows',     'il mio vicino Paolo', 'seed_social_role_it', 0.82),
  ('vicina',      'social_role', 'knows',     'la mia vicina Rosa',  'seed_social_role_it', 0.82),
  ('vicini',      'social_role', 'knows',     'i miei vicini',       'seed_social_role_it', 0.80),
  ('coinquilino', 'social_role', 'knows',     'il mio coinquilino',  'seed_social_role_it', 0.82),
  ('coinquilina', 'social_role', 'knows',     'la mia coinquilina',  'seed_social_role_it', 0.82),
  ('conoscente',  'social_role', 'knows',     'un conoscente',       'seed_social_role_it', 0.80)
ON CONFLICT (cue, category) DO NOTHING;

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('dose',        'dosage_noun',  'quantity',   'la mia dose di vitamina D è 1000 UI', 'seed_dosage_noun_it',  0.90),
  ('dosi',        'dosage_noun',  'quantity',   'le dosi sono due',                    'seed_dosage_noun_it',  0.85),
  ('dosaggio',    'dosage_noun',  'quantity',   'il dosaggio è 5 ml',                  'seed_dosage_noun_it',  0.85),
  ('quantità',    'dosage_noun',  'quantity',   'la quantità è 200 mg',                'seed_dosage_noun_it',  0.85),
  ('livello',     'dosage_noun',  'quantity',   'il mio livello di ferro è 40',        'seed_dosage_noun_it',  0.85),
  ('vocabolario', 'measure_noun', 'vocabulary', 'il suo vocabolario è di 300 parole',  'seed_measure_noun_it', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('anno',        'unit_scalar',     'age',      'ho 34 anni',                'seed_unit_scalar_it', 0.85),
  ('anni',        'unit_scalar',     'age',      'mia sorella ha 30 anni',    'seed_unit_scalar_it', 0.85),
  ('mese',        'unit_scalar',     'duration', 'dura un mese',              'seed_unit_scalar_it', 0.80),
  ('mesi',        'unit_scalar',     'duration', 'dura 3 mesi',               'seed_unit_scalar_it', 0.80),
  ('settimana',   'unit_scalar',     'duration', 'dura una settimana',        'seed_unit_scalar_it', 0.80),
  ('settimane',   'unit_scalar',     'duration', 'dura 2 settimane',          'seed_unit_scalar_it', 0.80),
  ('giorno',      'unit_scalar',     'duration', 'dura un giorno',            'seed_unit_scalar_it', 0.80),
  ('giorni',      'unit_scalar',     'duration', 'dura 5 giorni',             'seed_unit_scalar_it', 0.80),
  ('ora',         'unit_scalar',     'duration', 'dura un''ora',              'seed_unit_scalar_it', 0.80),
  ('ore',         'unit_scalar',     'duration', 'il film dura 2 ore',        'seed_unit_scalar_it', 0.80),
  ('minuto',      'unit_scalar',     'duration', 'dura un minuto',            'seed_unit_scalar_it', 0.80),
  ('minuti',      'unit_scalar',     'duration', 'dura 45 minuti',            'seed_unit_scalar_it', 0.80),
  ('secondo',     'unit_scalar',     'duration', 'dura un secondo',           'seed_unit_scalar_it', 0.75),
  ('secondi',     'unit_scalar',     'duration', 'dura 90 secondi',           'seed_unit_scalar_it', 0.80),
  ('metro',       'unit_scalar',     'height',   'è alto un metro',           'seed_unit_scalar_it', 0.80),
  ('metri',       'unit_scalar',     'height',   'è alto 2 metri',            'seed_unit_scalar_it', 0.80),
  ('centimetro',  'unit_scalar',     'height',   'un centimetro',             'seed_unit_scalar_it', 0.80),
  ('centimetri',  'unit_scalar',     'height',   'è alta 120 centimetri',     'seed_unit_scalar_it', 0.80),
  ('chilo',       'unit_scalar',     'weight',   'pesa un chilo',             'seed_unit_scalar_it', 0.80),
  ('chili',       'unit_scalar',     'weight',   'pesa 80 chili',             'seed_unit_scalar_it', 0.80),
  ('chilogrammo', 'unit_scalar',     'weight',   'pesa un chilogrammo',       'seed_unit_scalar_it', 0.80),
  ('chilogrammi', 'unit_scalar',     'weight',   'pesa 80 chilogrammi',       'seed_unit_scalar_it', 0.80),
  ('avere',       'possession_verb', 'Stative possession verb (it): the subject has the object', 'ho un cane', 'seed_possession_verb_it', 0.90),
  ('pesare',      'measure_verb',    'measurement verb (it): mass measure-phrase complement',   'pesa 80 chili',     'seed_measure_verb_it', 0.85),
  ('durare',      'measure_verb',    'measurement verb (it): duration measure-phrase complement', 'dura 2 ore',      'seed_measure_verb_it', 0.85),
  ('misurare',    'measure_verb',    'measurement verb (it): extent measure-phrase complement', 'misura 3 metri',    'seed_measure_verb_it', 0.85),
  ('lungo',       'dimension_adjective', 'length<-height', 'la corda è lunga 4 metri',        'seed_dimension_adjective_it', 0.85),
  ('alto',        'dimension_adjective', 'height<-height', 'è alta 120 centimetri',           'seed_dimension_adjective_it', 0.85),
  ('largo',       'dimension_adjective', 'width<-height',  'il tavolo è largo 90 centimetri', 'seed_dimension_adjective_it', 0.85),
  ('profondo',    'dimension_adjective', 'depth<-height',  'il lago è profondo 30 metri',     'seed_dimension_adjective_it', 0.85),
  ('durare',      'dimension_verb',      'duration<-duration,age', 'il corso dura 3 anni',    'seed_dimension_verb_it',      0.85),
  ('pesare',      'dimension_verb',      'weight<-weight',         'pesa 80 chili',           'seed_dimension_verb_it',      0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- SCALAR dimensions named by the dimension adjectives (absent from the seed ontology).
INSERT INTO public.rel_types
    (rel_type, label, head_types, tail_types, engine_generated, confidence, correction_behavior,
     source, category, is_symmetric, is_leaf_only, is_hierarchy_rel, storage_target, fact_class,
     natural_language, temporal_class, scalar_datatype)
SELECT v.rel_type, v.label, ARRAY['ANY'], ARRAY['SCALAR'], false, 1.0, 'supersede', 'builtin',
       'physical', false, false, false, 'facts', 'A', v.nl, 'state', 'quantity'
  FROM (VALUES ('length', 'Length', 'X è lungo Y'),
               ('width',  'Width',  'X è largo Y'),
               ('depth',  'Depth',  'X è profondo Y')) AS v(rel_type, label, nl)
 WHERE NOT EXISTS (SELECT 1 FROM public.rel_types r WHERE r.rel_type = v.rel_type);

-- Query-side KIN / SOCIAL-ROLE aliases (rel_type_aliases — determine_path's keyword→rel lane, the
-- SAME rail the English daughter→parent_of / mother→child_of rows ride). Without them "Chi è mia
-- figlia?" scopes no kin rel at all. Direction and inversion mirror the English rows exactly (a
-- noun naming the CHILD scopes parent_of; a noun naming the PARENT scopes child_of with
-- requires_inversion). UNIQUE on alias alone, so ON CONFLICT (alias).
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source, confidence, requires_inversion)
VALUES
  ('parent_of',  'figlio',   'it_seed', 0.95, false),
  ('parent_of',  'figlia',   'it_seed', 0.95, false),
  ('parent_of',  'figli',    'it_seed', 0.92, false),
  ('parent_of',  'figlie',   'it_seed', 0.92, false),
  ('child_of',   'madre',    'it_seed', 0.95, true),
  ('child_of',   'padre',    'it_seed', 0.95, true),
  ('child_of',   'mamma',    'it_seed', 0.92, true),
  ('child_of',   'papà',     'it_seed', 0.92, true),
  ('child_of',   'genitore', 'it_seed', 0.92, true),
  ('child_of',   'genitori', 'it_seed', 0.92, true),
  ('sibling_of', 'fratello', 'it_seed', 0.95, false),
  ('sibling_of', 'sorella',  'it_seed', 0.95, false),
  ('sibling_of', 'fratelli', 'it_seed', 0.92, false),
  ('sibling_of', 'sorelle',  'it_seed', 0.92, false),
  ('spouse',     'marito',   'it_seed', 0.95, false),
  ('spouse',     'moglie',   'it_seed', 0.95, false),
  ('friend_of',  'amico',    'it_seed', 0.90, false),
  ('friend_of',  'amica',    'it_seed', 0.90, false)
ON CONFLICT (alias) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas)
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
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = _schema AND table_name = 'linguistic_cues') THEN
            EXECUTE format($seed$
                INSERT INTO %I.linguistic_cues
                    (cue, category, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     source, is_active, archived_at, last_matched_at)
                SELECT cue, category, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       source, is_active, archived_at, last_matched_at
                FROM public.linguistic_cues
                WHERE source IN ('seed_naming_verb_it', 'seed_first_person_possessive_it',
                                 'seed_kinship_it', 'seed_social_role_it',
                                 'seed_dosage_noun_it', 'seed_measure_noun_it',
                                 'seed_unit_scalar_it', 'seed_possession_verb_it',
                                 'seed_measure_verb_it', 'seed_dimension_adjective_it',
                                 'seed_dimension_verb_it')
                ON CONFLICT (cue, category) DO NOTHING
            $seed$, _schema);
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = _schema AND table_name = 'rel_types') THEN
            EXECUTE format($rt$
                INSERT INTO %I.rel_types
                    (rel_type, label, head_types, tail_types, engine_generated, confidence,
                     correction_behavior, source, category, is_symmetric, is_leaf_only,
                     is_hierarchy_rel, storage_target, fact_class, natural_language,
                     temporal_class, scalar_datatype)
                SELECT p.rel_type, p.label, p.head_types, p.tail_types, p.engine_generated,
                       p.confidence, p.correction_behavior, p.source, p.category, p.is_symmetric,
                       p.is_leaf_only, p.is_hierarchy_rel, p.storage_target, p.fact_class,
                       p.natural_language, p.temporal_class, p.scalar_datatype
                FROM public.rel_types p
                WHERE p.rel_type IN ('length', 'width', 'depth')
                  AND NOT EXISTS (SELECT 1 FROM %I.rel_types r WHERE r.rel_type = p.rel_type)
            $rt$, _schema, _schema);
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = _schema AND table_name = 'rel_type_aliases') THEN
            EXECUTE format($al$
                INSERT INTO %I.rel_type_aliases
                    (canonical_rel_type, alias, source, confidence, requires_inversion)
                SELECT a.canonical_rel_type, a.alias, a.source, a.confidence, a.requires_inversion
                FROM public.rel_type_aliases a
                WHERE a.source = 'it_seed'
                  AND EXISTS (SELECT 1 FROM %I.rel_types r WHERE r.rel_type = a.canonical_rel_type)
                ON CONFLICT (alias) DO NOTHING
            $al$, _schema, _schema);
        END IF;
        RAISE NOTICE 'Migration 282: Italian engine cue seeds applied in %', _schema;
    END LOOP;
END $$;
